"""Offline resource check. Run this inside the production image with a memory limit."""

import json
from pathlib import Path
import resource
import time

from scripts.preview_entry_report import sample_assessment
from buysignal.analysis import signal_charts


def read_cgroup(name):
    path = Path('/sys/fs/cgroup') / name
    try:
        return path.read_text().strip()
    except OSError:
        return None


def main():
    assessment = sample_assessment()
    before = time.process_time()
    started = time.perf_counter()
    charts = [signal_charts.render_signal_chart(s) for s in assessment.subsignals]
    cold = time.perf_counter() - started
    cold_cpu = time.process_time() - before
    started = time.perf_counter()
    for _ in range(10):
        for signal in assessment.subsignals:
            signal_charts.render_signal_chart(signal)
    warm = (time.perf_counter() - started) / 10
    print(json.dumps({
        'charts': sum(p is not None for p in charts),
        'cold_wall_seconds': round(cold, 3), 'cold_cpu_seconds': round(cold_cpu, 3),
        'warm_report_seconds': round(warm, 4),
        'png_total_bytes': sum(len(p) for p in charts if p is not None),
        'chart_cache_bytes': signal_charts._CACHE_BYTES,
        'process_peak_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        'container_memory_peak_bytes': read_cgroup('memory.peak'),
        'container_memory_limit_bytes': read_cgroup('memory.max'),
        'cpu_limit': read_cgroup('cpu.max'),
    }, indent=2))


if __name__ == '__main__':
    main()
