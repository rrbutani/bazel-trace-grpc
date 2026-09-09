# bazel gRPC trace

CLI tool that renders Bazel's `--remote_grpc_log` output as a chrome trace, optionally merging with an existing trace (`--generate_json_trace_profile`).

---

<!-- ## what -->

<!-- (despite the name — `RemoteExecutionLog` — contains more than just remote execution traffic; includes bytestream API traffic, etc.) -->

<!-- ## why -->

## how do i use this

```console
❯ log2trace.py --help
usage: log2trace.py [-h] --grpc_log_path GRPC_LOG_PATH [--trace PATH] --output-path PATH

options:
  -h, --help            show this help message and exit
  --grpc_log_path, -l GRPC_LOG_PATH
                        `--remote_grpc_log` file from Bazel (length-delimited
                        protobuf messages)
  --trace, -t PATH      existing Bazel JSON chrome trace to merge gRPC events
                        into; plaintext JSON or gzip or zstd compressed
  --output-path, -o PATH
                        output path for chrome trace; zstd compressed JSON file
```

First run a Bazel command and collect a chrome trace and a gRPC log:
```console
❯ bazel_args=(
    --profile=bazel-profile.json.gz
    --generate_json_trace_profile
    --remote_grpc_log=grpc-log.bin
)
❯ bazel build ... "${bazel_args[@]}"
```

Next, merge the two with `log2trace.py`:
```console
❯ bazel run @bazel-trace-grpc -- -l grpc-log.bin -t bazel-profile.json.gz -o trace.json.zst
```

View with a chrome trace viewer, e.g. [perfetto](https://ui.perfetto.dev/).
