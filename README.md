# Website Chatbot Benchmark

This tool benchmarks website chatbots using customized questions and reference answers generated from website content.

One AI agent uses agent-browser to explore the website and create the benchmark. A separate runner agent asks the chatbot those questions, evaluates its replies, and produces a results report.

## Requirements:

Requires Python 3.11+, Node.js 20.19+ or 22.12+, and [OpenCode](https://opencode.ai).
Sign in to OpenCode and make sure your chosen models work before starting.

## Setup

```bash
git clone https://github.com/Gary-Community-Ventures-AI/website-chatbot-benchmark.git
cd website-chatbot-benchmark
python3 -m venv venv
source venv/bin/activate
pip install -e .
npm install -g agent-browser
agent-browser install
```

## Create a benchmark

```bash
benchmark new my-site
```

Edit `benchmarks/my-site/config.yaml`. Set the website URL, models, and any instructions.

## Generate and run

```bash
benchmark my-site generate
benchmark my-site run
```

For a quick first try, add `--limit 5` to the generate command.
To replace an existing benchmark, add `--force`.

## View results

```bash
benchmark view
```

Opens http://127.0.0.1:5555. Results load automatically.
The first launch installs and builds the frontend. Press Ctrl+C to stop.

In a new terminal, run `source venv/bin/activate` before using `benchmark`.
