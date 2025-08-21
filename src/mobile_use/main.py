import asyncio
import multiprocessing
import platform
import sys
import time
from adbutils import AdbClient
import requests
from pathlib import Path
from typing import Optional

import typer
from langchain_core.messages import AIMessage
from rich.console import Console
from typing_extensions import Annotated

from mobile_use.agents.outputter.outputter import outputter
from mobile_use.clients.device_hardware_client import DeviceHardwareClient
from mobile_use.clients.screen_api_client import (
    ScreenApiClient,
    get_client as get_screen_api_client,
)
from mobile_use.config import (
    OutputConfig,
    initialize_llm_config,
    prepare_output_files,
    record_events,
    settings,
)
from mobile_use.constants import (
    RECURSION_LIMIT,
)
from mobile_use.context import DeviceContext, DevicePlatform, ExecutionSetup, MobileUseContext
from mobile_use.controllers.mobile_command_controller import ScreenDataResponse, get_screen_data
from mobile_use.controllers.platform_specific_commands_controller import get_first_device_id
from mobile_use.graph.graph import get_graph
from mobile_use.graph.state import State
from mobile_use.llm_config_context import LLMConfigContext, set_llm_config_context
from mobile_use.servers.config import server_settings
from mobile_use.servers.device_hardware_bridge import BridgeStatus
from mobile_use.servers.start_servers import (
    start_device_hardware_bridge,
    start_device_screen_api,
)
from mobile_use.servers.stop_servers import stop_servers
from mobile_use.utils.cli_helpers import display_device_status
from mobile_use.utils.logger import get_logger
from mobile_use.utils.media import (
    create_gif_from_trace_folder,
    create_steps_json_from_trace_folder,
    remove_images_from_trace_folder,
    remove_steps_json_from_trace_folder,
)
from mobile_use.utils.recorder import log_agent_thoughts
from mobile_use.utils.time import convert_timestamp_to_str

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
logger = get_logger(__name__)


def print_ai_response_to_stderr(graph_result: State):
    for msg in reversed(graph_result.messages):
        if isinstance(msg, AIMessage):
            print(msg.content, file=sys.stderr)
            return


def check_device_screen_api_health_with_retry_logic(client: ScreenApiClient) -> bool:
    restart_screen_api = not settings.DEVICE_SCREEN_API_BASE_URL
    restart_hw_bridge = not settings.DEVICE_HARDWARE_BRIDGE_BASE_URL

    try:
        client.get_with_retry("/health", timeout=5)
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Device Screen API health check failed: {e}")
        stop_servers(
            device_screen_api=restart_screen_api,
            device_hardware_bridge=restart_hw_bridge,
        )
        return False


def run_servers(device_id: str, screen_api_client: ScreenApiClient) -> bool:
    """
    Starts all required servers, waits for them to be ready,
    and returns the device ID if available.
    """
    api_process = None

    if not settings.DEVICE_HARDWARE_BRIDGE_BASE_URL:
        bridge_instance = start_device_hardware_bridge(device_id)
        if not bridge_instance:
            logger.warning("Failed to start Device Hardware Bridge. Exiting.")
            logger.info(
                "Note: Device Screen API requires Device Hardware Bridge to function properly."
            )
            return False

        logger.info("Waiting for Device Hardware Bridge to connect to a device...")
        while True:
            status_info = bridge_instance.get_status()
            status = status_info.get("status")
            output = status_info.get("output")

            if status == BridgeStatus.RUNNING.value:
                logger.success(
                    f"Device Hardware Bridge is running. Connected to device: {device_id}"
                )
                break

            failed_statuses = [
                BridgeStatus.NO_DEVICE.value,
                BridgeStatus.FAILED.value,
                BridgeStatus.PORT_IN_USE.value,
                BridgeStatus.STOPPED.value,
            ]
            if status in failed_statuses:
                logger.error(
                    f"Device Hardware Bridge failed to connect. Status: {status} - Output: {output}"
                )
                return False

            time.sleep(1)

    if not settings.DEVICE_SCREEN_API_BASE_URL:
        api_process = start_device_screen_api(use_process=True)
        if not api_process or not isinstance(api_process, multiprocessing.Process):
            logger.error("Failed to start Device Screen API. Exiting.")
            return False

    if not check_device_screen_api_health_with_retry_logic(client=screen_api_client):
        logger.error("Device Screen API health check failed after retries. Stopping...")
        if api_process:
            api_process.terminate()
        return False

    return True


def get_mobile_use_context(
    device_id: str,
    screen_api_client: ScreenApiClient,
    adb_client: Optional[AdbClient] = None,
) -> MobileUseContext:
    hw_bridge_client = DeviceHardwareClient(
        base_url=server_settings.DEVICE_HARDWARE_BRIDGE_BASE_URL,
    )

    host_platform = platform.system()
    screen_data: ScreenDataResponse = get_screen_data(screen_api_client)
    device_context = DeviceContext(
        host_platform="WINDOWS" if host_platform == "Windows" else "LINUX",
        mobile_platform=DevicePlatform.ANDROID
        if screen_data.platform == "ANDROID"
        else DevicePlatform.IOS,
        device_id=device_id,
        device_width=screen_data.width,
        device_height=screen_data.height,
    )

    return MobileUseContext(
        device=device_context,
        hw_bridge_client=hw_bridge_client,
        screen_api_client=screen_api_client,
        adb_client=adb_client,
    )


async def run_automation(
    goal: str,
    test_name: Optional[str] = None,
    traces_output_path_str: str = "traces",
    graph_config_callbacks: Optional[list] = [],
    output_config: Optional[OutputConfig] = None,
    adb_client: Optional[AdbClient] = None,
):
    device_id: str | None = None
    events_output_path, results_output_path = prepare_output_files()

    screen_api_client = get_screen_api_client(base_url=settings.DEVICE_SCREEN_API_BASE_URL)

    logger.info("⚙️ Starting Mobile-use servers...")
    max_restart_attempts = 3
    restart_attempt = 0

    device_id = get_first_device_id()
    if not device_id:
        logger.error("❌ No device found. Exiting.")
        return

    while restart_attempt < max_restart_attempts:
        success = run_servers(device_id=device_id, screen_api_client=screen_api_client)
        if success:
            break

        restart_attempt += 1
        if restart_attempt < max_restart_attempts:
            logger.warning(
                f"Server start failed, attempting restart {restart_attempt}/{max_restart_attempts}"
            )
            time.sleep(3)
        else:
            logger.error(
                "❌ Mobile-use servers failed to start after all restart attempts. Exiting."
            )
            return

    llm_config = initialize_llm_config()
    set_llm_config_context(LLMConfigContext(llm_config=llm_config))
    logger.info(str(llm_config))

    context = get_mobile_use_context(
        device_id=device_id, screen_api_client=screen_api_client, adb_client=adb_client
    )
    logger.info(context.device.to_str())

    start_time = time.time()
    trace_id: str | None = None
    traces_temp_path: Path | None = None
    traces_output_path: Path | None = None
    structured_output: dict | None = None

    if test_name:
        traces_output_path = Path(traces_output_path_str).resolve()
        logger.info(f"📂 Traces output path: {traces_output_path}")
        traces_temp_path = Path(__file__).parent.joinpath(f"../traces/{test_name}").resolve()
        logger.info(f"📄📂 Traces temp path: {traces_temp_path}")
        traces_output_path.mkdir(parents=True, exist_ok=True)
        traces_temp_path.mkdir(parents=True, exist_ok=True)
        trace_id = test_name
        context.execution_setup = ExecutionSetup(trace_id=trace_id)

    logger.info(f"Starting graph with goal: `{goal}`")
    if output_config and output_config.needs_structured_format():
        logger.info(str(output_config))
    graph_input = State(
        messages=[],
        initial_goal=goal,
        subgoal_plan=[],
        latest_ui_hierarchy=None,
        latest_screenshot_base64=None,
        focused_app_info=None,
        device_date=None,
        structured_decisions=None,
        agents_thoughts=[],
        remaining_steps=RECURSION_LIMIT,
        executor_retrigger=False,
        executor_failed=False,
        executor_messages=[],
        cortex_last_thought=None,
    ).model_dump()

    success = False
    last_state: State | None = None
    try:
        logger.info(f"Invoking graph with input: {graph_input}")
        async for chunk in (await get_graph(context)).astream(
            input=graph_input,
            config={
                "recursion_limit": RECURSION_LIMIT,
                "callbacks": graph_config_callbacks,
            },
            stream_mode=["messages", "custom", "values"],
        ):
            stream_mode, content = chunk
            if stream_mode == "values":
                last_state = State(**content)  # type: ignore
                log_agent_thoughts(
                    agents_thoughts=last_state.agents_thoughts,
                    events_output_path=events_output_path,
                )
        if not last_state:
            logger.warning("No result received from graph")
            return

        print_ai_response_to_stderr(graph_result=last_state)
        if output_config and output_config.needs_structured_format():
            logger.info("Generating structured output...")
            try:
                structured_output = await outputter(
                    output_config=output_config, graph_output=last_state
                )
            except Exception as e:
                logger.error(f"Failed to generate structured output: {e}")
                structured_output = None

        logger.info("✅ Automation is success ✅")
        success = True
    except Exception as e:
        logger.error(f"Error running automation: {e}")
        raise
    finally:
        if traces_temp_path and traces_output_path and start_time:
            formatted_ts = convert_timestamp_to_str(start_time)
            status = "_PASS" if success else "_FAIL"
            new_name = f"{test_name}{status}_{formatted_ts}"

            logger.info("Compiling trace FROM FOLDER: " + str(traces_temp_path))
            create_gif_from_trace_folder(traces_temp_path)
            create_steps_json_from_trace_folder(traces_temp_path)

            logger.info("Video created, removing dust...")
            remove_images_from_trace_folder(traces_temp_path)
            remove_steps_json_from_trace_folder(traces_temp_path)
            logger.info("📽️ Trace compiled, moving to output path 📽️")

            output_folder_path = traces_temp_path.rename(traces_output_path / new_name)
            logger.info(f"📂✅ Trace folder renamed to: {output_folder_path.name}")

        await asyncio.sleep(1)
    if structured_output:
        logger.info(f"Structured output: {structured_output}")
        record_events(output_path=results_output_path, events=structured_output)
        return structured_output
    if last_state and last_state.agents_thoughts:
        last_msg = last_state.agents_thoughts[-1]
        logger.info(str(last_msg))
        record_events(output_path=results_output_path, events=last_msg)
        return last_msg
    return None


@app.command()
def main(
    goal: Annotated[str, typer.Argument(help="The main goal for the agent to achieve.")],
    test_name: Annotated[
        Optional[str],
        typer.Option(
            "--test-name",
            "-n",
            help="A name for the test recording. If provided, a trace will be saved.",
        ),
    ] = None,
    traces_path: Annotated[
        str,
        typer.Option(
            "--traces-path",
            "-p",
            help="The path to save the traces.",
        ),
    ] = "traces",
    output_description: Annotated[
        Optional[str],
        typer.Option(
            "--output-description",
            "-o",
            help=(
                """
                A dict output description for the agent.
                Ex: a JSON schema with 2 keys: type, price
                """
            ),
        ),
    ] = None,
):
    """
    Run the Mobile-use agent to automate tasks on a mobile device.
    """
    console = Console()
    adb_client = AdbClient(
        host=settings.ADB_HOST or "localhost",
        port=settings.ADB_PORT or 5037,
    )
    display_device_status(console, adb_client=adb_client)
    output_config = None
    if output_description:
        output_config = OutputConfig(output_description=output_description, structured_output=None)
    asyncio.run(
        run_automation(
            goal=goal,
            test_name=test_name,
            traces_output_path_str=traces_path,
            output_config=output_config,
            adb_client=adb_client,
        )
    )


def cli():
    app()


if __name__ == "__main__":
    cli()
