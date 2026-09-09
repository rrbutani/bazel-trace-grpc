"""Script to convert Bazel's `--remote_grpc_log` to a Chrome trace, optionally merging
it with an existing Bazel Chrome trace (`--generate_json_trace_profile`).
"""

import argparse
from dataclasses import dataclass
from collections.abc import Callable, Generator, Iterator
from compression import zstd, gzip
import io
import itertools
import json
from pathlib import Path
import shutil
import sys

from google.protobuf.proto import parse_length_prefixed
from google.protobuf.json_format import MessageToDict
from remote_execution_log_pb2 import LogEntry

#---------------------------------------------------------------------------------------

def parse_grpc_log(file: str) -> Generator[LogEntry]:
    with open(file, "rb") as g:
        while g.peek(1) != b'':
            yield parse_length_prefixed(LogEntry, g)

def mk_grpc_events(
    trace: ExistingTrace | None,
    log_iter: Iterator[LogEntry],
) -> Generator[object]:
    first_ev = next(log_iter, None)
    if not first_ev:
        print("warning: no events in gRPC log", file=sys.stderr)
        yield from []
        return

    # check that `bazel_version`, `build_id` in existing trace matches gRPC log events
    other_data = trace.toplevel_keys.get("otherData", {}) if trace else {}
    if bzl_ver := other_data.get("bazel_version"):
        tool_details = first_ev.metadata.tool_details
        assert bzl_ver.startswith("release "), f"{bzl_ver}"
        assert tool_details.tool_name == "bazel", f"{tool_details.tool_name}"
        bzl_ver = bzl_ver.removeprefix("release ")
        if tool_details.tool_version != bzl_ver:
            print(
                "warning: existing trace and gRPC log disagree about the Bazel version "
                "used to capture the trace/log:\n"
                f"  - trace bazel version:    {bzl_ver}\n"
                f"  - gRPC log bazel version: {tool_details.tool_version}\n"
                "\n"
                "are you using gRPC log & trace files from the same Bazel invocation?",
                "\n",
                file=sys.stderr,
            )
    if trace_build_id := other_data.get("build_id"):
        log_tool_invocation_id = first_ev.metadata.tool_invocation_id
        if trace_build_id != log_tool_invocation_id:
            print(
                "warning: existing trace and gRPC log disagree about the invocation ID "
                "they correspond to:\n"
                f"  - build invocation ID for trace:    {trace_build_id}\n"
                f"  - build invocation ID for gRPC log: {log_tool_invocation_id}\n"
                "\n"
                "are you using gRPC log & trace files from the same Bazel invocation?",
                "\n",
                file=sys.stderr,
            )

    trace_start_time_ms = 0
    if trace:
        if ts := other_data.get("profile_start_ts", None):
            trace_start_time_ms = ts

            first_start = first_ev.start_time.ToMilliseconds()
            assert ts <= first_start, (
                "expect original trace to begin before gRPC log events: "
                f"{ts} <= {first_start}"
            )
        else:
            print(
                "warning: missing `otherData.profile_start_ts` in original trace; "
                "trace events from gRPC log will likely be out of sync with events in"
                "the original trace",
                file=sys.stderr,
            )
    trace_start_time_us = 1000 * trace_start_time_ms

    def _name_supplement(name: str, ev: LogEntry) -> str:
        if tgt := ev.metadata.target_id:
            name += f": {tgt}"
            if mnemonic := ev.metadata.action_mnemonic:
                name += f" ({mnemonic})"
        else:
            name += f": {ev.metadata.action_id}"
        return name

    # see: https://docs.google.com/document/u/0/d/1CvAClvFfyA5R-PhYUmn5OOQtYMH4h6I0nSsKchNAySU
    ids = dict(pid=1, tid=999991)
    yield dict(name="thread_name", ph="M", args=dict(name="gRPC Events"), **ids)
    for ev in itertools.chain([first_ev], log_iter):
        # `cname`: see: https://github.com/catapult-project/catapult/blob/5c5e5a1d285e906479d8cd506db17cce519e6a3b/tracing/tracing/base/color_scheme.html#L29-L72
        match ev.method_name:
            case "build.bazel.remote.execution.v2.Capabilities/GetCapabilities":
                name = "GetCapabilities"
                cname = "startup"
            # TODO: Execute
            case "build.bazel.remote.execution.v2.ActionCache/GetActionResult":
                name = _name_supplement("GetActionResult", ev)
                cname = "grey"
            # TODO: UpdateActionResult
            # TODO: WaitExecution
            # TODO: FindMissingBlobs
            # TODO: SplitBlob
            # TODO: SpliceBlob
            case "google.bytestream.ByteStream/Read":
                name = _name_supplement("ByteStream/Read", ev)
                cname = "grey"
            # TODO: Write
            # TODO: QueryWriteStatus
            case _:
                cname = "generic_work"
                name = ev.method_name

        yield dict(
            cat=f"gRPC,{ev.method_name}",
            name=name,
            cname=cname,
            ph="X",
            ts=ev.start_time.ToMicroseconds() - trace_start_time_us,
            dur=ev.end_time.ToMicroseconds() - ev.start_time.ToMicroseconds(),
            args=MessageToDict(ev),
            **ids,
        )

@dataclass
class ExistingTrace:
    toplevel_keys: dict[str, object]
    inject_events_as_json_lines: Callable[[io.BinaryIO], None]
    "note: contract is that this function assumes prev events have a trailing comma"

def parse_existing_trace(path: str) -> ExistingTrace:
    # NOTE: relying on formatting that's specific to Bazel's chrome traces to avoid
    # having to deserialize the whole trace:
    f = open(path, 'rb')

    magic = f.read(4)
    f.seek(0)

    # see: https://en.wikipedia.org/wiki/List_of_file_signatures
    match magic:
        case b'\x28\xb5\x2f\xfd':
            f = zstd.ZstdFile(f)
        case m if m.startswith(b'\x1f\x8b'):
            f = gzip.GzipFile(fileobj=f)
        case _:
            # assume it's plain text JSON:
            pass

    # read the first line, check if it's what we expect from a Bazel-generated trace:
    first = f.readline()
    if (
        first.startswith(b'{"otherData":{"bazel_version":') and
        first.endswith(b',"traceEvents":[\n')
    ):
        toplevel_other = json.loads(first.removesuffix(b',"traceEvents":[\n') + b"}")
        def inject_events(sink: io.BinaryIO):
            shutil.copyfileobj(f, sink)
            f.close()
    else:
        print(
            f"warning: original trace ({path}) does not appear to be produced by "
            "Bazel; falling back to deserializing full trace\n"
            f"  - first line does not match expected pattern; got: {first}\n",
            file=sys.stderr,
        )
        f.seek(0)
        full_trace = json.load(f)
        f.close()
        toplevel_other = { k: v for k, v in full_trace.items() if k != "traceEvents" }
        def inject_events(sink: io.BinaryIO):
            tsink = io.TextIOWrapper(sink, encoding='utf-8')
            for i, ev in enumerate(full_trace["traceEvents"]):
                if i != 0:
                    tsink.write(",\n    ")
                json.dump(ev, tsink)
            tsink.write("\n  ]\n}")
            tsink.detach()

    return ExistingTrace(toplevel_other, inject_events)

#---------------------------------------------------------------------------------------

def argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--grpc_log_path", "-l", type=str, metavar="GRPC_LOG_PATH", required=True,
        help="`--remote_grpc_log` file from Bazel (length-delimited protobuf messages)",
    )
    parser.add_argument(
        "--trace", "-t", type=str, metavar="PATH", required=False,
        help=(
            "existing Bazel JSON chrome trace to merge gRPC events into; plaintext "
            "JSON or gzip or zstd compressed"
        ),
    )
    parser.add_argument(
        "--output-path", "-o", type=str, metavar="PATH", required=True,
        help=(
            "output path for chrome trace; zstd compressed JSON file"
        )
    )
    return parser

def main(args: list[str]) -> int:
    parsed = argparser().parse_args(args)

    orig = None
    if (orig_trace := parsed.trace):
        orig = parse_existing_trace(orig_trace)

    # emit the new trace, merging in events from the old trace (if present):
    out = Path(parsed.output_path)
    out.parent.mkdir(exist_ok=True)
    with zstd.ZstdFile(out, 'wb') as out:
        toplevel = orig.toplevel_keys if orig else dict(otherData = {})
        first_line = json.dumps(toplevel)
        out.write(first_line[:-1].encode())
        out.write(b',"traceEvents":[\n')

        # write out new events:
        out_t, first = io.TextIOWrapper(out, encoding='utf-8'), True
        for new_event in mk_grpc_events(orig, parse_grpc_log(parsed.grpc_log_path)):
            if not first:
                out_t.write(",\n    ")
            first = False
            json.dump(new_event, out_t)
        out_t.detach()

        if orig:
            # note: orig cannot have no events or else there will be an invalid trailing
            # comma...
            #
            # (the gRPC log must also have at least one event)
            out.write(b',\n')
            orig.inject_events_as_json_lines(out)
        else:
            out.write(b'\n  ]')
            out.write(b'\n}')
            out.write(b'\n')

    return 0

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

#---------------------------------------------------------------------------------------

# TODO: knob to omit args for smaller output traces?
# TODO: attempt to put gRPC events on different threads (TIDs)
