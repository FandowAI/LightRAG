#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
server="$project_dir/.venv/bin/lightrag-server"
log_file="$project_dir/lightrag-server.log"
port=9621

if [[ ! -x "$server" ]]; then
    echo "LightRAG server executable not found: $server" >&2
    exit 1
fi

mapfile -t listener_pids < <(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u)
for pid in "${listener_pids[@]}"; do
    command_line="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    if [[ "$command_line" != *lightrag-server* ]]; then
        echo "Port $port is used by a process other than lightrag-server (PID $pid)." >&2
        exit 1
    fi
done

if ((${#listener_pids[@]})); then
    echo "Stopping LightRAG on port $port..."
    kill "${listener_pids[@]}"
    for _ in {1..30}; do
        if ! lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "Port $port is still occupied; startup aborted." >&2
        exit 1
    fi
fi

cd "$project_dir"
echo "Starting LightRAG in the background..."
nohup "$server" --host 0.0.0.0 --port "$port" >> "$log_file" 2>&1 < /dev/null &
new_pid=$!

for _ in {1..45}; do
    if curl --noproxy '*' -fsS --max-time 2 \
        "http://127.0.0.1:$port/health" > /dev/null 2>&1; then
        echo "LightRAG is healthy (PID $new_pid)."
        echo "WebUI: http://10.10.115.253:$port/webui/"
        echo "Logs: $log_file and $project_dir/lightrag.log"
        exit 0
    fi
    if ! kill -0 "$new_pid" 2>/dev/null; then
        break
    fi
    sleep 1
done

echo "LightRAG did not become healthy; recent startup log:" >&2
tail -n 40 "$log_file" >&2
exit 1
