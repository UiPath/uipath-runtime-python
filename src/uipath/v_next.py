import abc
from contextvars import ContextVar
from typing import Generic, TypeVar, ContextManager

import logging
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor, SynchronousMultiSpanProcessor, Span
from pydantic import BaseModel
from typing_extensions import override

I = TypeVar("I")
O = TypeVar("O")

logger = logging.getLogger(__name__)

class UiPathRuntime(abc.ABC, Generic[I, O]):
    @abc.abstractmethod
    def execute(self, input: I) -> O:
        """
        Execute the agent.
        """
        raise NotImplementedError()


class MyAgentInput(BaseModel):
    query: str

class MyAgentOutput(BaseModel):
    response: str


class MyAgent(UiPathRuntime[MyAgentInput, MyAgentOutput]):
    @override
    def execute(self, input: MyAgentInput) -> MyAgentOutput:
        print("Running agent with input:", input)
        return MyAgentOutput(response="AgentExecuted!")

class MultiSpanProcessor(SynchronousMultiSpanProcessor):
    def remove_span_processor(self, span_processor: SpanProcessor) -> None:
        with self._lock:
            temp_list = list(self._span_processors)
            temp_list.remove(span_processor)
            self._span_processors = tuple(temp_list)

uipath_multi_span_processor_context = ContextVar("uipath_multi_span_processor")
class SpanProcessorContext(ContextManager):
    def __init__(self, span_processor: SpanProcessor):
        self.span_processor = span_processor

    def __enter__(self):
        uipath_multi_span_processor:MultiSpanProcessor  = uipath_multi_span_processor_context.get()
        if uipath_multi_span_processor:
            uipath_multi_span_processor.add_span_processor(self.span_processor)
        else:
            print("WARNING: CALL INIT!")

    def __exit__(self, exc_type, exc_val, exc_tb):
        uipath_multi_span_processor = uipath_multi_span_processor_context.get()
        if uipath_multi_span_processor:
            uipath_multi_span_processor.remove_span_processor(self.span_processor)
            self.span_processor.shutdown()


def add_span_processor(span_processor: SpanProcessor) -> SpanProcessorContext:
    return SpanProcessorContext(span_processor)


def init():
    # setup tracer
    tracer_provider = TracerProvider()
    span_processor = MultiSpanProcessor()
    tracer_provider.add_span_processor(span_processor)
    trace.set_tracer_provider(tracer_provider)
    uipath_multi_span_processor_context.set(span_processor)



if __name__ == '__main__':
    # in uipath base command (applicable to everything)
    # invoke uipath_init() which sets up tracers and what not.
    init()

    # in CLI-run
    entrypoint = "v_next.py:MyAgent"
    input_json = {"query": "query!"}
    # Note: input needs to be converted into correct object type.
    input_object = MyAgentInput(**input_json)
    # agent = RuntimeFactory.load(entrypoint) # this initializes the agent using contructor.
    agent = MyAgent()

    class ConsoleLoggerProcessor(SpanProcessor):
        def on_start(self, span: Span, parent_context=None):
            print("Started span input:", span)

    # Note: eval can add UiPathRuntimeExecutionSpanExporter for evaluation purposes.
    with add_span_processor(ConsoleLoggerProcessor()) as span_exporter:
        tracer = trace.get_tracer("uipath-runtime")
        with tracer.start_as_current_span(
            name="root",
            attributes={},
        ):
            result = agent.execute(input_object)
            print("Agent result:",result)
