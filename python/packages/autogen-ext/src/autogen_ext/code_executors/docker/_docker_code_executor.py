# File based from: https://github.com/microsoft/autogen/blob/main/autogen/coding/docker_commandline_code_executor.py
# Credit to original authors

from __future__ import annotations

import asyncio
import logging
import shlex
import sys
import tempfile
import uuid
import warnings
from collections.abc import Sequence
from concurrent.futures import Future as ConcurrentFuture
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional, ParamSpec, Tuple, Union

from autogen_core import CancellationToken, Component
from autogen_core.code_executor import (
    CodeBlock,
    CodeExecutor,
    FunctionWithRequirements,
    FunctionWithRequirementsStr,
)
from pydantic import BaseModel
from typing_extensions import Self

from docker.types import DeviceRequest

from .._common import (
    CommandLineCodeResult,
    build_python_functions_file,
    get_file_name_from_content,
    lang_to_cmd,
    silence_pip,
)

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

try:
    import asyncio_atexit

    import docker
    from docker.errors import DockerException, ImageNotFound, NotFound
    from docker.models.containers import Container
except ImportError as e:
    raise RuntimeError(
        "Missing dependecies for DockerCommandLineCodeExecutor. Please ensure the autogen-ext package was installed with the 'docker' extra."
    ) from e


async def _wait_for_ready(container: Any, timeout: int = 60, stop_time: float = 0.1) -> None:
    elapsed_time = 0.0
    while container.status != "running" and elapsed_time < timeout:
        await asyncio.sleep(stop_time)
        elapsed_time += stop_time
        await asyncio.to_thread(container.reload)
        continue
    if container.status != "running":
        raise ValueError("Container failed to start")


A = ParamSpec("A")


class DockerCommandLineCodeExecutorConfig(BaseModel):
    """Configuration for DockerCommandLineCodeExecutor"""

    image: str = "python:3-slim"
    container_name: Optional[str] = None
    timeout: int = 60
    work_dir: Optional[str] = None
    bind_dir: Optional[str] = None
    auto_remove: bool = True
    stop_container: bool = True
    functions_module: str = "functions"
    extra_volumes: Dict[str, Dict[str, str]] = {}
    extra_hosts: Dict[str, str] = {}
    init_command: Optional[str] = None
    delete_tmp_files: bool = False


class DockerCommandLineCodeExecutor(CodeExecutor, Component[DockerCommandLineCodeExecutorConfig]):
    """Executes code through a command line environment in a Docker container.

    .. note::

        This class requires the :code:`docker` extra for the :code:`autogen-ext` package:

        .. code-block:: bash

            pip install "autogen-ext[docker]"


    The executor first saves each code block in a file in the working
    directory, and then executes the code file in the container.
    The executor executes the code blocks in the order they are received.
    Currently, the executor only supports Python and shell scripts.
    For Python code, use the language "python" for the code block.
    For shell scripts, use the language "bash", "shell", "sh", "pwsh", "powershell", or "ps1" for the code block.

    Args:
        image (_type_, optional): Docker image to use for code execution.
            Defaults to "python:3-slim".
        container_name (Optional[str], optional): Name of the Docker container
            which is created. If None, will autogenerate a name. Defaults to None.
        timeout (int, optional): The timeout for code execution. Defaults to 60.
        work_dir (Union[Path, str], optional): The working directory for the code
            execution. Defaults to temporary directory.
        bind_dir (Union[Path, str], optional): The directory that will be bound
        to the code executor container. Useful for cases where you want to spawn
        the container from within a container. Defaults to work_dir.
        auto_remove (bool, optional): If true, will automatically remove the Docker
            container when it is stopped. Defaults to True.
        stop_container (bool, optional): If true, will automatically stop the
            container when stop is called, when the context manager exits or when
            the Python process exits with atext. Defaults to True.
        device_requests (Optional[List[DeviceRequest]], optional): A list of device request instances to add to the container for exposing GPUs (e.g., [docker.types.DeviceRequest(count=-1, capabilities=[['gpu']])]). Defaults to None.
        functions (List[Union[FunctionWithRequirements[Any, A], Callable[..., Any]]]): A list of functions that are available to the code executor. Default is an empty list.
        functions_module (str, optional): The name of the module that will be created to store the functions. Defaults to "functions".
        extra_volumes (Optional[Dict[str, Dict[str, str]]], optional): A dictionary of extra volumes (beyond the work_dir) to mount to the container;
            key is host source path and value 'bind' is the container path. See  Defaults to None.
            Example: extra_volumes = {'/home/user1/': {'bind': '/mnt/vol2', 'mode': 'rw'}, '/var/www': {'bind': '/mnt/vol1', 'mode': 'ro'}}
        extra_hosts (Optional[Dict[str, str]], optional): A dictionary of host mappings to add to the container. (See Docker docs on extra_hosts) Defaults to None.
            Example: extra_hosts = {"kubernetes.docker.internal": "host-gateway"}
        init_command (Optional[str], optional): A shell command to run before each shell operation execution. Defaults to None.
            Example: init_command="kubectl config use-context docker-hub"
        delete_tmp_files (bool, optional): If true, will delete temporary files after execution. Defaults to False.

    .. note::
        Using the current directory (".") as working directory is deprecated. Using it will raise a deprecation warning.

    """

    component_config_schema = DockerCommandLineCodeExecutorConfig
    component_provider_override = "autogen_ext.code_executors.docker.DockerCommandLineCodeExecutor"

    SUPPORTED_LANGUAGES: ClassVar[List[str]] = [
        "bash",
        "shell",
        "sh",
        "pwsh",
        "powershell",
        "ps1",
        "python",
    ]

    FUNCTION_PROMPT_TEMPLATE: ClassVar[
        str
    ] = """You have access to the following user defined functions. They can be accessed from the module called `$module_name` by their function names.

For example, if there was a function called `foo` you could import it by writing `from $module_name import foo`

$functions"""

    def __init__(
        self,
        image: str = "python:3-slim",
        container_name: Optional[str] = None,
        *,
        timeout: int = 60,
        work_dir: Union[Path, str, None] = None,
        bind_dir: Optional[Union[Path, str]] = None,
        auto_remove: bool = True,
        stop_container: bool = True,
        device_requests: Optional[List[DeviceRequest]] = None,
        functions: Sequence[
            Union[
                FunctionWithRequirements[Any, A],
                Callable[..., Any],
                FunctionWithRequirementsStr,
            ]
        ] = [],
        functions_module: str = "functions",
        extra_volumes: Optional[Dict[str, Dict[str, str]]] = None,
        extra_hosts: Optional[Dict[str, str]] = None,
        init_command: Optional[str] = None,
        delete_tmp_files: bool = False,
    ):
        if timeout < 1:
            raise ValueError("Timeout must be greater than or equal to 1.")

        # Handle working directory logic
        if work_dir is None:
            self._work_dir = None
        else:
            if isinstance(work_dir, str):
                work_dir = Path(work_dir)
            # Emit a deprecation warning if the user is using the current directory as working directory
            if work_dir.resolve() == Path.cwd().resolve():
                warnings.warn(
                    "Using the current directory as work_dir is deprecated.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            self._work_dir = work_dir
            # Create the working directory if it doesn't exist
            self._work_dir.mkdir(exist_ok=True, parents=True)

        if container_name is None:
            self.container_name = f"autogen-code-exec-{uuid.uuid4()}"
        else:
            self.container_name = container_name

        self._timeout = timeout

        # Handle bind_dir
        self._bind_dir: Optional[Path] = None
        if bind_dir is not None:
            self._bind_dir = Path(bind_dir) if isinstance(bind_dir, str) else bind_dir
        else:
            self._bind_dir = self._work_dir  # Default to work_dir if not provided

        # Track temporary directory
        self._temp_dir: Optional[tempfile.TemporaryDirectory[str]] = None
        self._temp_dir_path: Optional[Path] = None

        self._started = False

        self._auto_remove = auto_remove
        self._stop_container = stop_container
        self._image = image

        if not functions_module.isidentifier():
            raise ValueError("Module name must be a valid Python identifier")

        self._functions_module = functions_module
        self._functions = functions
        self._extra_volumes = extra_volumes if extra_volumes is not None else {}
        self._extra_hosts = extra_hosts if extra_hosts is not None else {}
        self._init_command = init_command
        self._delete_tmp_files = delete_tmp_files
        self._device_requests = device_requests

        # Setup could take some time so we intentionally wait for the first code block to do it.
        if len(functions) > 0:
            self._setup_functions_complete = False
        else:
            self._setup_functions_complete = True

        self._container: Container | None = None
        self._running = False

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._cancellation_futures: List[ConcurrentFuture[None]] = []

    @property
    def timeout(self) -> int:
        """(Experimental) The timeout for code execution."""
        return self._timeout

    async def _setup_functions(self, cancellation_token: CancellationToken) -> None:
        func_file_content = build_python_functions_file(self._functions)
        func_file = self.work_dir / f"{self._functions_module}.py"
        func_file.write_text(func_file_content)

        # Collect requirements
        lists_of_packages = [x.python_packages for x in self._functions if isinstance(x, FunctionWithRequirements)]
        flattened_packages = [item for sublist in lists_of_packages for item in sublist]
        required_packages = list(set(flattened_packages))
        if len(required_packages) > 0:
            logging.info("Ensuring packages are installed in executor.")

            packages = shlex.join(required_packages)

            result = await self._execute_code_dont_check_setup(
                [CodeBlock(code=f"python -m pip install {packages}", language="sh")], cancellation_token
            )

            if result.exit_code != 0:
                stdout = result.output
                stderr = result.output
                raise ValueError(f"Pip install failed. {stdout}, {stderr}")

        # Attempt to load the function file to check for syntax errors, imports etc.
        exec_result = await self._execute_code_dont_check_setup(
            [CodeBlock(code=func_file_content, language="python")], cancellation_token
        )

        if exec_result.exit_code != 0:
            raise ValueError(f"Functions failed to load: {exec_result.output}")

        self._setup_functions_complete = True

    async def _kill_running_command(self, command: List[str]) -> None:
        if self._container is None or not self._running:
            return
        await asyncio.to_thread(self._container.exec_run, ["pkill", "-f", " ".join(command)])

    async def _execute_command(self, command: List[str], cancellation_token: CancellationToken) -> Tuple[str, int]:
        if self._container is None or not self._running:
            raise ValueError("Container is not running. Must first be started with either start or a context manager.")

        exec_task = asyncio.create_task(asyncio.to_thread(self._container.exec_run, command))
        cancellation_token.link_future(exec_task)

        # Wait for the exec task to finish.
        try:
            result = await exec_task
            exit_code = result.exit_code
            output = result.output.decode("utf-8")
            if exit_code == 124:
                output += "\n Timeout"
            return output, exit_code
        except asyncio.CancelledError:
            # Schedule a task to kill the running command in the background.
            if self._loop and not self._loop.is_closed():
                try:
                    logging.debug(f"Scheduling kill command via run_coroutine_threadsafe on loop {self._loop!r}")
                    future: ConcurrentFuture[None] = asyncio.run_coroutine_threadsafe(
                        self._kill_running_command(command), self._loop
                    )
                    self._cancellation_futures.append(future)
                    logging.debug(f"Kill command scheduled, future: {future!r}")
                except RuntimeError as e:
                    logging.error(f"Failed to schedule kill command on loop {self._loop!r}: {e}")
                except Exception as e:
                    logging.exception(f"Unexpected error scheduling kill command: {e}")
            else:
                logging.warning(
                    f"Cannot schedule kill command: Executor loop is not available or closed (loop: {self._loop!r})."
                )
            return "Code execution was cancelled.", 1

    async def _execute_code_dont_check_setup(
        self, code_blocks: List[CodeBlock], cancellation_token: CancellationToken
    ) -> CommandLineCodeResult:
        if self._container is None or not self._running:
            raise ValueError("Container is not running. Must first be started with either start or a context manager.")

        if len(code_blocks) == 0:
            raise ValueError("No code blocks to execute.")

        outputs: List[str] = []
        files: List[Path] = []
        last_exit_code = 0
        try:
            for code_block in code_blocks:
                lang = code_block.language.lower()
                code = silence_pip(code_block.code, lang)

                # Check if there is a filename comment
                try:
                    filename = get_file_name_from_content(code, self.work_dir)
                except ValueError:
                    outputs.append("Filename is not in the workspace")
                    last_exit_code = 1
                    break

                if not filename:
                    filename = f"tmp_code_{sha256(code.encode()).hexdigest()}.{lang}"

                code_path = self.work_dir / filename
                with code_path.open("w", encoding="utf-8") as fout:
                    fout.write(code)
                files.append(code_path)
                try:
                    command = ["timeout", str(self._timeout), lang_to_cmd(lang), filename]
                except ValueError as e:
                    outputs.append(str(e))
                    last_exit_code = 1
                    break

                output, exit_code = await self._execute_command(command, cancellation_token)
                outputs.append(output)
                last_exit_code = exit_code
                if exit_code != 0:
                    break
        finally:
            if self._delete_tmp_files:
                for file in files:
                    try:
                        file.unlink()
                    except (OSError, FileNotFoundError):
                        pass

        code_file = str(files[0]) if files else None
        return CommandLineCodeResult(exit_code=last_exit_code, output="".join(outputs), code_file=code_file)

    @property
    def work_dir(self) -> Path:
        # If a user specifies a working directory, use that
        if self._work_dir is not None:
            # If a user specifies the current directory, warn them that this is deprecated
            if self._work_dir == Path("."):
                warnings.warn(
                    "Using the current directory as work_dir is deprecated.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            return self._work_dir
        # If a user does not specify a working directory, use the default directory (tempfile.TemporaryDirectory)
        elif self._temp_dir is not None:
            return Path(self._temp_dir.name)
        else:
            raise RuntimeError("Working directory not properly initialized")

    @property
    def bind_dir(self) -> Path:
        # If the user specified a bind directory, return it
        if self._bind_dir is not None:
            return self._bind_dir
        # Otherwise bind_dir is set to the current work_dir as default
        else:
            return self.work_dir

    async def execute_code_blocks(
        self, code_blocks: List[CodeBlock], cancellation_token: CancellationToken
    ) -> CommandLineCodeResult:
        """(Experimental) Execute the code blocks and return the result.

        Args:
            code_blocks (List[CodeBlock]): The code blocks to execute.

        Returns:
            CommandlineCodeResult: The result of the code execution."""

        if not self._setup_functions_complete:
            await self._setup_functions(cancellation_token)

        return await self._execute_code_dont_check_setup(code_blocks, cancellation_token)

    async def restart(self) -> None:
        """(Experimental) Restart the Docker container code executor."""
        if self._container is None or not self._running:
            raise ValueError("Container is not running. Must first be started with either start or a context manager.")

        await asyncio.to_thread(self._container.restart)  # type: ignore
        if self._container.status != "running":
            self._running = False
            logs_str = self._container.logs().decode("utf-8")
            raise ValueError(f"Failed to restart container. Logs: {logs_str}")

    async def stop(self) -> None:
        """(Experimental) Stop the code executor.

        Stops the Docker container and cleans up any temporary files (if they were created), along with the temporary directory.
        The method first waits for all cancellation tasks to finish before stopping the container. Finally it marks the executor as not running.
        If the container is not running, the method does nothing.
        """
        if not self._running:
            return

        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None

        client = docker.from_env()
        try:
            try:
                container = await asyncio.to_thread(client.containers.get, self.container_name)
            except NotFound:
                logging.debug(f"Container {self.container_name} not found during stop...")
                self._running = False
                self._cancellation_futures.clear()
                return

            if self._cancellation_futures:
                if not self._loop or self._loop.is_closed():
                    logging.warning(
                        f"Executor loop ({self._loop!r}) is closed or unavailable. Cannot reliably wait for "
                        f"{len(self._cancellation_futures)} cancellation futures."
                    )
                    self._cancellation_futures.clear()
                else:
                    # concurrent.futures.Future -> asyncio.Future
                    asyncio_futures = [asyncio.wrap_future(f, loop=self._loop) for f in self._cancellation_futures]

                    if asyncio_futures:
                        logging.debug(
                            f"Waiting for {len(asyncio_futures)} cancellation futures to complete on loop {self._loop!r}..."
                        )
                        results = await asyncio.gather(*asyncio_futures, return_exceptions=True)
                        for i, result in enumerate(results):
                            original_future = self._cancellation_futures[i]
                            if isinstance(result, Exception):
                                logging.warning(f"Cancellation future {original_future!r} failed: {result}")
                            else:
                                logging.debug(f"Cancellation future {original_future!r} completed successfully.")
                    else:
                        logging.debug("No valid cancellation futures to await.")

                    self._cancellation_futures.clear()

            logging.debug(f"Stopping container {self.container_name}...")
            await asyncio.to_thread(container.stop)
            logging.debug(f"Container {self.container_name} stopped.")

        except DockerException as e:
            logging.error(f"Docker error while stopping container {self.container_name}: {e}")
        except Exception as e:
            logging.exception(f"Unexpected error during stop operation for container {self.container_name}: {e}")
        finally:
            self._running = False
            self._cancellation_futures.clear()

    async def start(self) -> None:
        """(Experimental) Start the code executor.

        This method sets the working environment variables, connects to Docker and starts the code executor.
        If no working directory was provided to the code executor, it creates a temporary directory and sets it as the code executor working directory.
        """

        if self._work_dir is None and self._temp_dir is None:
            self._temp_dir = tempfile.TemporaryDirectory()
            self._temp_dir_path = Path(self._temp_dir.name)
            self._temp_dir_path.mkdir(exist_ok=True)

        # Start a container from the image, read to exec commands later
        try:
            client = docker.from_env()
        except DockerException as e:
            if "FileNotFoundError" in str(e):
                raise RuntimeError("Failed to connect to Docker. Please ensure Docker is installed and running.") from e
            raise
        except Exception as e:
            raise RuntimeError(f"Unexpected error while connecting to Docker: {str(e)}") from e

        # Check if the image exists
        try:
            await asyncio.to_thread(client.images.get, self._image)
        except ImageNotFound:
            # TODO logger
            logging.info(f"Pulling image {self._image}...")
            # Let the docker exception escape if this fails.
            await asyncio.to_thread(client.images.pull, self._image)

        # Prepare the command (if needed)
        shell_command = "/bin/sh"
        command = ["-c", f"{(self._init_command)};exec {shell_command}"] if self._init_command else None

        # Check if a container with the same name already exists and remove it
        try:
            existing_container = await asyncio.to_thread(client.containers.get, self.container_name)
            await asyncio.to_thread(existing_container.remove, force=True)
        except NotFound:
            pass

        self._container = await asyncio.to_thread(
            client.containers.create,
            self._image,
            name=self.container_name,
            entrypoint=shell_command,
            command=command,
            tty=True,
            detach=True,
            auto_remove=self._auto_remove,
            volumes={str(self.bind_dir.resolve()): {"bind": "/workspace", "mode": "rw"}, **self._extra_volumes},
            working_dir="/workspace",
            extra_hosts=self._extra_hosts,
            device_requests=self._device_requests,
        )
        await asyncio.to_thread(self._container.start)

        await _wait_for_ready(self._container)

        async def cleanup() -> None:
            await self.stop()
            asyncio_atexit.unregister(cleanup)  # type: ignore

        if self._stop_container:
            asyncio_atexit.register(cleanup)  # type: ignore

        # Check if the container is running
        if self._container.status != "running":
            logs_str = self._container.logs().decode("utf-8")
            raise ValueError(f"Failed to start container from image {self._image}. Logs: {logs_str}")

        self._loop = asyncio.get_running_loop()
        self._cancellation_futures = []
        logging.debug(f"Executor started, associated with event loop: {self._loop!r}")

        self._running = True

    def _to_config(self) -> DockerCommandLineCodeExecutorConfig:
        """(Experimental) Convert the component to a config object."""
        if self._functions:
            logging.info("Functions will not be included in serialized configuration")

        return DockerCommandLineCodeExecutorConfig(
            image=self._image,
            container_name=self.container_name,
            timeout=self._timeout,
            work_dir=str(self._work_dir) if self._work_dir else None,
            bind_dir=str(self._bind_dir) if self._bind_dir else None,
            auto_remove=self._auto_remove,
            stop_container=self._stop_container,
            functions_module=self._functions_module,
            extra_volumes=self._extra_volumes,
            extra_hosts=self._extra_hosts,
            init_command=self._init_command,
            delete_tmp_files=self._delete_tmp_files,
        )

    @classmethod
    def _from_config(cls, config: DockerCommandLineCodeExecutorConfig) -> Self:
        """(Experimental) Create a component from a config object."""

        return cls(
            image=config.image,
            container_name=config.container_name,
            timeout=config.timeout,
            work_dir=Path(config.work_dir) if config.work_dir else None,
            bind_dir=Path(config.bind_dir) if config.bind_dir else None,
            auto_remove=config.auto_remove,
            stop_container=config.stop_container,
            functions=[],  # Functions not restored from config
            functions_module=config.functions_module,
            extra_volumes=config.extra_volumes,
            extra_hosts=config.extra_hosts,
            init_command=config.init_command,
            delete_tmp_files=config.delete_tmp_files,
        )
