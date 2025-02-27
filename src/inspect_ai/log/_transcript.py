import asyncio
import contextlib
from contextvars import ContextVar
from datetime import datetime
from logging import getLogger
from typing import (
    Any,
    Iterator,
    Literal,
    Sequence,
    Type,
    TypeAlias,
    TypeVar,
    Union,
)

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_serializer

from inspect_ai._util.constants import SAMPLE_SUBTASK
from inspect_ai._util.error import EvalError
from inspect_ai._util.json import JsonChange, json_changes
from inspect_ai._util.working import sample_working_time
from inspect_ai.dataset._dataset import Sample
from inspect_ai.log._message import LoggingMessage
from inspect_ai.model._chat_message import ChatMessage
from inspect_ai.model._generate_config import GenerateConfig
from inspect_ai.model._model_call import ModelCall
from inspect_ai.model._model_output import ModelOutput
from inspect_ai.scorer._metric import Score
from inspect_ai.solver._task_state import state_jsonable
from inspect_ai.tool._tool import ToolResult
from inspect_ai.tool._tool_call import (
    ToolCall,
    ToolCallContent,
    ToolCallError,
    ToolCallView,
)
from inspect_ai.tool._tool_choice import ToolChoice
from inspect_ai.tool._tool_info import ToolInfo
from inspect_ai.util._store import store, store_changes, store_jsonable

logger = getLogger(__name__)


class BaseEvent(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.now)
    """Clock time at which event occurred."""

    working_start: float = Field(default_factory=sample_working_time)
    """Working time (within sample) at which the event occurred."""

    pending: bool | None = Field(default=None)
    """Is this event pending?"""

    @field_serializer("timestamp")
    def serialize_timestamp(self, dt: datetime) -> str:
        return dt.astimezone().isoformat()


class SampleInitEvent(BaseEvent):
    """Beginning of processing a Sample."""

    event: Literal["sample_init"] = Field(default="sample_init")
    """Event type."""

    sample: Sample
    """Sample."""

    state: JsonValue
    """Initial state."""


class SampleLimitEvent(BaseEvent):
    """The sample was unable to finish processing due to a limit"""

    event: Literal["sample_limit"] = Field(default="sample_limit")
    """Event type."""

    type: Literal["message", "time", "working", "token", "operator", "custom"]
    """Type of limit that halted processing"""

    message: str
    """A message associated with this limit"""

    limit: int | None = Field(default=None)
    """The limit value (if any)"""


class StoreEvent(BaseEvent):
    """Change to data within the current `Store`."""

    event: Literal["store"] = Field(default="store")
    """Event type."""

    changes: list[JsonChange]
    """List of changes to the `Store`."""


class StateEvent(BaseEvent):
    """Change to the current `TaskState`"""

    event: Literal["state"] = Field(default="state")
    """Event type."""

    changes: list[JsonChange]
    """List of changes to the `TaskState`"""


class ModelEvent(BaseEvent):
    """Call to a language model."""

    event: Literal["model"] = Field(default="model")
    """Event type."""

    model: str
    """Model name."""

    input: list[ChatMessage]
    """Model input (list of messages)."""

    tools: list[ToolInfo]
    """Tools available to the model."""

    tool_choice: ToolChoice
    """Directive to the model which tools to prefer."""

    config: GenerateConfig
    """Generate config used for call to model."""

    output: ModelOutput
    """Output from model."""

    error: str | None = Field(default=None)
    """Error which occurred during model call."""

    cache: Literal["read", "write"] | None = Field(default=None)
    """Was this a cache read or write."""

    call: ModelCall | None = Field(default=None)
    """Raw call made to model API."""

    completed: datetime | None = Field(default=None)
    """Time that model call completed (see `timestamp` for started)"""

    working_time: float | None = Field(default=None)
    """working time for model call that succeeded (i.e. was not retried)."""

    @field_serializer("completed")
    def serialize_completed(self, dt: datetime) -> str:
        if dt is None:
            return None
        return dt.astimezone().isoformat()


class ToolEvent(BaseEvent):
    """Call to a tool."""

    event: Literal["tool"] = Field(default="tool")
    """Event type."""

    type: Literal["function"] = Field(default="function")
    """Type of tool call (currently only 'function')"""

    id: str
    """Unique identifier for tool call."""

    function: str
    """Function called."""

    arguments: dict[str, JsonValue]
    """Arguments to function."""

    view: ToolCallContent | None = Field(default=None)
    """Custom view of tool call input."""

    result: ToolResult = Field(default_factory=str)
    """Function return value."""

    truncated: tuple[int, int] | None = Field(default=None)
    """Bytes truncated (from,to) if truncation occurred"""

    error: ToolCallError | None = Field(default=None)
    """Error that occurred during tool call."""

    events: list["Event"] = Field(default_factory=list)
    """Transcript of events for tool."""

    completed: datetime | None = Field(default=None)
    """Time that tool call completed (see `timestamp` for started)"""

    working_time: float | None = Field(default=None)
    """Working time for tool call (i.e. time not spent waiting on semaphores)."""

    def _set_result(
        self,
        result: ToolResult,
        truncated: tuple[int, int] | None,
        error: ToolCallError | None,
        events: list["Event"],
        waiting_time: float,
    ) -> None:
        self.result = result
        self.truncated = truncated
        self.error = error
        self.events = events
        self.pending = None
        completed = datetime.now()
        self.completed = completed
        self.working_time = (completed - self.timestamp).total_seconds() - waiting_time

    # mechanism for operator to cancel the tool call

    def _set_task(self, task: asyncio.Task[Any]) -> None:
        """Set the tool task (for possible cancellation)"""
        self._task = task

    def _cancel(self) -> None:
        """Cancel the tool task."""
        if self._task:
            self._cancelled = True
            self._task.cancel()

    @property
    def cancelled(self) -> bool:
        """Was the task cancelled?"""
        return self._cancelled is True

    _cancelled: bool | None = None
    """Was this tool call cancelled?"""

    _task: asyncio.Task[Any] | None = None
    """Handle to task (used for cancellation)"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    """Required so that we can include '_task' as a member."""

    @field_serializer("completed")
    def serialize_completed(self, dt: datetime) -> str:
        return dt.astimezone().isoformat()


class SandboxEvent(BaseEvent):
    """Sandbox execution or I/O"""

    event: Literal["sandbox"] = Field(default="sandbox")
    """Event type"""

    action: Literal["exec", "read_file", "write_file"]
    """Sandbox action"""

    cmd: str | None = Field(default=None)
    """Command (for exec)"""

    options: dict[str, JsonValue] | None = Field(default=None)
    """Options (for exec)"""

    file: str | None = Field(default=None)
    """File (for read_file and write_file)"""

    input: str | None = Field(default=None)
    """Input (for cmd and write_file). Truncated to 100 lines."""

    result: int | None = Field(default=None)
    """Result (for exec)"""

    output: str | None = Field(default=None)
    """Output (for exec and read_file). Truncated to 100 lines."""

    completed: datetime | None = Field(default=None)
    """Time that sandbox action completed (see `timestamp` for started)"""

    @field_serializer("completed")
    def serialize_completed(self, dt: datetime) -> str:
        return dt.astimezone().isoformat()


class ApprovalEvent(BaseEvent):
    """Tool approval."""

    event: Literal["approval"] = Field(default="approval")
    """Event type"""

    message: str
    """Message generated by model along with tool call."""

    call: ToolCall
    """Tool call being approved."""

    view: ToolCallView | None = Field(default=None)
    """View presented for approval."""

    approver: str
    """Aprover name."""

    decision: Literal["approve", "modify", "reject", "escalate", "terminate"]
    """Decision of approver."""

    modified: ToolCall | None = Field(default=None)
    """Modified tool call for decision 'modify'."""

    explanation: str | None = Field(default=None)
    """Explanation for decision."""


class InputEvent(BaseEvent):
    """Input screen interaction."""

    event: Literal["input"] = Field(default="input")
    """Event type."""

    input: str
    """Input interaction (plain text)."""

    input_ansi: str
    """Input interaction (ANSI)."""


class LoggerEvent(BaseEvent):
    """Log message recorded with Python logger."""

    event: Literal["logger"] = Field(default="logger")
    """Event type."""

    message: LoggingMessage
    """Logging message"""


class InfoEvent(BaseEvent):
    """Event with custom info/data."""

    event: Literal["info"] = Field(default="info")
    """Event type."""

    source: str | None = Field(default=None)
    """Optional source for info event."""

    data: JsonValue
    """Data provided with event."""


class ErrorEvent(BaseEvent):
    """Event with sample error."""

    event: Literal["error"] = Field(default="error")
    """Event type."""

    error: EvalError
    """Sample error"""


class ScoreEvent(BaseEvent):
    """Event with score.

    Can be the final score for a `Sample`, or can be an intermediate score
    resulting from a call to `score`.
    """

    event: Literal["score"] = Field(default="score")
    """Event type."""

    score: Score
    """Score value."""

    target: str | list[str] | None = Field(default=None)
    """"Sample target."""

    intermediate: bool = Field(default=False)
    """Was this an intermediate scoring?"""


class StepEvent(BaseEvent):
    """Step within current sample or subtask."""

    event: Literal["step"] = Field(default="step")
    """Event type."""

    action: Literal["begin", "end"]
    """Designates beginning or end of event."""

    type: str | None = Field(default=None)
    """Optional 'type' field for events"""

    name: str
    """Event name."""


class SubtaskEvent(BaseEvent):
    """Subtask spawned."""

    event: Literal["subtask"] = Field(default="subtask")
    """Event type."""

    name: str
    """Name of subtask function."""

    type: str | None = Field(default=None)
    """Type of subtask"""

    input: dict[str, Any]
    """Subtask function inputs."""

    result: Any = Field(default=None)
    """Subtask function result."""

    events: list["Event"] = Field(default_factory=list)
    """Transcript of events for subtask."""

    completed: datetime | None = Field(default=None)
    """Time that subtask completed (see `timestamp` for started)"""

    working_time: float | None = Field(default=None)
    """Working time for subtask (i.e. time not spent waiting on semaphores or model retries)."""

    @field_serializer("completed")
    def serialize_completed(self, dt: datetime) -> str:
        return dt.astimezone().isoformat()


Event: TypeAlias = Union[
    SampleInitEvent
    | SampleLimitEvent
    | SandboxEvent
    | StateEvent
    | StoreEvent
    | ModelEvent
    | ToolEvent
    | SandboxEvent
    | ApprovalEvent
    | InputEvent
    | ScoreEvent
    | ErrorEvent
    | LoggerEvent
    | InfoEvent
    | StepEvent
    | SubtaskEvent,
]
"""Event in a transcript."""

ET = TypeVar("ET", bound=BaseEvent)


class Transcript:
    """Transcript of events."""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._events: list[Event] = []

    def info(self, data: JsonValue, *, source: str | None = None) -> None:
        """Add an `InfoEvent` to the transcript.

        Args:
           data: Data associated with the event.
           source: Optional event source.
        """
        self._event(InfoEvent(source=source, data=data))

    @contextlib.contextmanager
    def step(self, name: str, type: str | None = None) -> Iterator[None]:
        """Context manager for recording StepEvent.

        Args:
            name (str): Step name.
            type (str | None): Optional step type.
        """
        # step event
        self._event(StepEvent(action="begin", name=name, type=type))

        # run the step (tracking state/store changes)
        with track_state_changes(type), track_store_changes():
            yield

        # end step event
        self._event(StepEvent(action="end", name=name, type=type))

    @property
    def events(self) -> Sequence[Event]:
        return self._events

    def find_last_event(self, event_cls: Type[ET]) -> ET | None:
        for event in reversed(self.events):
            if isinstance(event, event_cls):
                return event
        return None

    def _event(self, event: Event) -> None:
        self._events.append(event)


def transcript() -> Transcript:
    """Get the current `Transcript`."""
    return _transcript.get()


@contextlib.contextmanager
def track_store_changes() -> Iterator[None]:
    before = store_jsonable(store())
    yield
    after = store_jsonable(store())

    changes = store_changes(before, after)
    if changes:
        transcript()._event(StoreEvent(changes=changes))


@contextlib.contextmanager
def track_state_changes(type: str | None = None) -> Iterator[None]:
    # we only want to track for step() inside the the sample
    # (solver level tracking is handled already and there are
    # no state changes in subtasks)
    if transcript().name == SAMPLE_SUBTASK and type != "solver":
        before = state_jsonable()
        yield
        after = state_jsonable()

        changes = json_changes(before, after)
        if changes:
            transcript()._event(StateEvent(changes=changes))
    else:
        yield


def init_transcript(transcript: Transcript) -> None:
    _transcript.set(transcript)


_transcript: ContextVar[Transcript] = ContextVar(
    "subtask_transcript", default=Transcript()
)
