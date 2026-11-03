"""
Master Assessment Test Runner & Verification Suite
Executes all 4 assessment tasks and generates a structured summary report.
"""

import sys
import os
import subprocess
import time

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(PROJECT_DIR, ".venv", "bin", "python3")
VENV_PYTEST = os.path.join(PROJECT_DIR, ".venv", "bin", "pytest")

TASKS = [
    ("Task 1: MCP Server with Strict Validation & Transport", "task1_mcp_server/test_task1.py"),
    ("Task 2: MCP Security Gateway Proxy (Tool Filtering & Auth)", "task2_mcp_gateway/test_task2.py"),
    ("Task 3: LLM Gateway Streaming Guardrail (PII Redaction)", "task3_streaming_guardrail/test_task3.py"),
    ("Task 4: Rate Limiting & Model Fallback Router (SQLite)", "task4_resilient_router/test_task4.py"),
]


def main():
    print("=" * 80)
    print(" AI Field Developer / Solutions Engineer Assessment Tasks")
    print(" Master Verification Suite")
    print("=" * 80)
    print(f"Project Directory: {PROJECT_DIR}")
    print(f"Python Interpreter: {VENV_PYTHON}\n")

    overall_passed = True
    results = []

    start_total = time.time()

    for task_name, test_file in TASKS:
        print(f"\n[RUNNING] {task_name}...")
        test_path = os.path.join(PROJECT_DIR, test_file)

        t0 = time.time()
        cmd = [VENV_PYTEST, test_path, "-v", "--no-header"]
        proc = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True)
        duration = time.time() - t0

        if proc.returncode == 0:
            print(f"[PASSED]  {task_name} ({duration:.2f}s)")
            results.append((task_name, "PASSED", duration, proc.stdout))
        else:
            print(f"[FAILED]  {task_name} ({duration:.2f}s)")
            print(proc.stdout)
            print(proc.stderr)
            results.append((task_name, "FAILED", duration, proc.stderr + proc.stdout))
            overall_passed = False

    total_duration = time.time() - start_total

    print("\n" + "=" * 80)
    print(" ASSESSMENT SUMMARY REPORT")
    print("=" * 80)
    for task_name, status, duration, _ in results:
        status_str = f"✅ {status}" if status == "PASSED" else f"❌ {status}"
        print(f"{status_str:<12} | {task_name:<60} | {duration:.2f}s")
    print("-" * 80)
    print(f"Total Execution Time: {total_duration:.2f}s")
    print("=" * 80)

    if overall_passed:
        print("\n🎉 ALL 4 ASSESSMENT TASKS VERIFIED AND PASSED SUCCESSFULLY!\n")
        sys.exit(0)
    else:
        print("\n❌ SOME TASKS FAILED. See log output above.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
