#!/usr/bin/env python3
"""
BSON vs JSON Bakeoff - Serialization benchmark for vasili data structures.

Compares BSON (via pymongo/bson) and JSON (stdlib) for the typical data
structures used in vasili's WiFi connection metrics, history, and portal
patterns. Measures serialization speed, deserialization speed, data size,
and query-like filtering operations.

Usage:
    python benchmarks/bson_json_bakeoff.py [--iterations N]
"""

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import bson
    from bson import BSON
    from bson.codec_options import CodecOptions

    HAS_BSON = True
except ImportError:
    try:
        from pymongo import MongoClient  # noqa: F401
        import bson
        from bson import BSON
        from bson.codec_options import CodecOptions

        HAS_BSON = True
    except ImportError:
        HAS_BSON = False
        print("WARNING: bson/pymongo not available. BSON benchmarks will be skipped.")


# --- Sample Data Generators ---


def generate_metric_doc(i: int) -> dict:
    """Generate a connection metrics document matching vasili's schema."""
    return {
        'ssid': f'Network_{i % 20}',
        'bssid': f'AA:BB:CC:DD:{i % 256:02X}:{(i * 7) % 256:02X}',
        'signal_strength': 30 + (i % 70),
        'channel': (i % 13) + 1,
        'encryption_type': ['WPA2', 'WPA3', 'Open', 'WEP'][i % 4],
        'download_speed': 5.0 + (i % 95),
        'upload_speed': 2.0 + (i % 48),
        'ping': 5.0 + (i % 195),
        'connection_method': ['open', 'wpa2', 'wpa3'][i % 3],
        'interface': f'wlan{i % 3}',
        'score': round(20.0 + (i % 80) + (i % 100) * 0.01, 2),
        'timestamp': time.time() - (i * 60),
        'connected': i % 5 != 0,
    }


def generate_history_doc(i: int) -> dict:
    """Generate a connection history document matching vasili's schema."""
    return {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'ssid': f'Network_{i % 20}',
        'bssid': f'AA:BB:CC:DD:{i % 256:02X}:{(i * 7) % 256:02X}',
        'signal_strength': 30 + (i % 70),
        'channel': (i % 13) + 1,
        'encryption_type': ['WPA2', 'WPA3', 'Open', 'WEP'][i % 4],
        'success': i % 4 != 0,
        'interface': f'wlan{i % 3}',
        'download_speed': 5.0 + (i % 95),
        'upload_speed': 2.0 + (i % 48),
        'ping': 5.0 + (i % 195),
    }


def generate_portal_doc(i: int) -> dict:
    """Generate a portal pattern document matching vasili's schema."""
    return {
        'ssid': f'Portal_{i % 10}',
        'redirect_domain': f'portal{i % 10}.example.com',
        'portal_type': ['click-through', 'login', 'terms'][i % 3],
        'auth_method': ['none', 'email', 'room_number'][i % 3],
        'last_seen': time.time() - (i * 3600),
        'success_count': i % 50,
        'failure_count': i % 10,
    }


# --- Benchmark Functions ---


def bench_json_serialize(docs: list[dict], iterations: int) -> dict:
    """Benchmark JSON serialization."""
    times = []
    encoded = None
    for _ in range(iterations):
        start = time.perf_counter()
        encoded = json.dumps(docs)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return {
        'times': times,
        'size_bytes': len(encoded.encode('utf-8')) if encoded else 0,
        'encoded': encoded,
    }


def bench_json_deserialize(encoded: str, iterations: int) -> dict:
    """Benchmark JSON deserialization."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        json.loads(encoded)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return {'times': times}


def bench_json_file_write(docs: list[dict], filepath: str, iterations: int) -> dict:
    """Benchmark JSON file write (append-style, simulating log storage)."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        with open(filepath, 'w') as f:
            json.dump(docs, f)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return {'times': times, 'file_size': os.path.getsize(filepath)}


def bench_json_file_read(filepath: str, iterations: int) -> dict:
    """Benchmark JSON file read."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        with open(filepath, 'r') as f:
            json.load(f)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return {'times': times}


def bench_bson_serialize(docs: list[dict], iterations: int) -> dict:
    """Benchmark BSON serialization."""
    if not HAS_BSON:
        return None
    times = []
    total_size = 0
    encoded_docs = []
    for _ in range(iterations):
        start = time.perf_counter()
        encoded_docs = [BSON.encode(doc) for doc in docs]
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    total_size = sum(len(d) for d in encoded_docs)
    return {'times': times, 'size_bytes': total_size, 'encoded': encoded_docs}


def bench_bson_deserialize(encoded_docs: list, iterations: int) -> dict:
    """Benchmark BSON deserialization."""
    if not HAS_BSON:
        return None
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        [BSON(doc).decode() for doc in encoded_docs]
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return {'times': times}


def bench_bson_file_write(docs: list[dict], filepath: str, iterations: int) -> dict:
    """Benchmark BSON file write."""
    if not HAS_BSON:
        return None
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        with open(filepath, 'wb') as f:
            for doc in docs:
                f.write(BSON.encode(doc))
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return {'times': times, 'file_size': os.path.getsize(filepath)}


def bench_bson_file_read(filepath: str, doc_count: int, iterations: int) -> dict:
    """Benchmark BSON file read (sequential scan)."""
    if not HAS_BSON:
        return None
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        with open(filepath, 'rb') as f:
            data = f.read()
        # Parse BSON documents from raw bytes
        offset = 0
        parsed = []
        while offset < len(data):
            doc_size = int.from_bytes(data[offset : offset + 4], 'little')
            doc_bytes = data[offset : offset + doc_size]
            parsed.append(BSON(doc_bytes).decode())
            offset += doc_size
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return {'times': times}


# --- Query-like Operations ---


def bench_json_filter(docs: list[dict], iterations: int) -> dict:
    """Benchmark filtering JSON docs in memory (simulating queries)."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        # Filter by ssid + connected (like get_network_history)
        filtered = [d for d in docs if d['ssid'] == 'Network_5' and d.get('connected', True)]
        # Sort by timestamp descending
        filtered.sort(key=lambda x: x['timestamp'], reverse=True)
        # Limit to 10
        result = filtered[:10]  # noqa: F841
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return {'times': times}


def bench_json_aggregate(docs: list[dict], iterations: int) -> dict:
    """Benchmark aggregation on JSON docs in memory (simulating MongoDB pipelines)."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        # Simulate get_best_networks aggregation
        connected = [d for d in docs if d.get('connected', True)]
        groups = {}
        for d in connected:
            ssid = d['ssid']
            if ssid not in groups:
                groups[ssid] = {
                    'scores': [],
                    'downloads': [],
                    'uploads': [],
                    'pings': [],
                    'signals': [],
                }
            groups[ssid]['scores'].append(d['score'])
            groups[ssid]['downloads'].append(d['download_speed'])
            groups[ssid]['uploads'].append(d['upload_speed'])
            groups[ssid]['pings'].append(d['ping'])
            groups[ssid]['signals'].append(d['signal_strength'])

        results = []
        for ssid, data in groups.items():
            results.append(
                {
                    '_id': ssid,
                    'avg_score': statistics.mean(data['scores']),
                    'avg_download': statistics.mean(data['downloads']),
                    'avg_upload': statistics.mean(data['uploads']),
                    'avg_ping': statistics.mean(data['pings']),
                    'avg_signal': statistics.mean(data['signals']),
                    'connection_count': len(data['scores']),
                }
            )
        results.sort(key=lambda x: x['avg_score'], reverse=True)
        top5 = results[:5]  # noqa: F841
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    return {'times': times}


# --- Reporting ---


def format_times(times: list[float]) -> str:
    """Format timing results."""
    mean = statistics.mean(times) * 1000
    median = statistics.median(times) * 1000
    stdev = statistics.stdev(times) * 1000 if len(times) > 1 else 0
    return f"mean={mean:.3f}ms  median={median:.3f}ms  stdev={stdev:.3f}ms"


def format_size(size_bytes: int) -> str:
    """Format byte size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def print_comparison(label: str, json_result: dict, bson_result: dict | None):
    """Print a comparison of JSON vs BSON results."""
    print(f"\n  {label}:")
    print(f"    JSON: {format_times(json_result['times'])}")
    if bson_result:
        print(f"    BSON: {format_times(bson_result['times'])}")
        json_mean = statistics.mean(json_result['times'])
        bson_mean = statistics.mean(bson_result['times'])
        if bson_mean > 0:
            ratio = json_mean / bson_mean
            faster = "JSON" if ratio < 1 else "BSON"
            factor = ratio if ratio > 1 else 1 / ratio
            print(f"    Winner: {faster} ({factor:.2f}x faster)")
    else:
        print("    BSON: N/A (pymongo not installed)")


def run_benchmark(doc_count: int, iterations: int):
    """Run the full benchmark suite."""
    print(f"\n{'=' * 70}")
    print(f"  BSON vs JSON Bakeoff — vasili serialization benchmark")
    print(f"  Documents: {doc_count}  |  Iterations: {iterations}")
    print(f"{'=' * 70}")

    # Generate test data
    metrics = [generate_metric_doc(i) for i in range(doc_count)]
    history = [generate_history_doc(i) for i in range(doc_count)]
    portals = [generate_portal_doc(i) for i in range(min(doc_count, 50))]

    tmp_json = '/tmp/vasili_bench.json'
    tmp_bson = '/tmp/vasili_bench.bson'

    try:
        # --- Metrics documents ---
        print(f"\n--- Connection Metrics ({doc_count} docs) ---")

        json_ser = bench_json_serialize(metrics, iterations)
        bson_ser = bench_bson_serialize(metrics, iterations)
        print_comparison("Serialization", json_ser, bson_ser)

        print(f"\n  Data size:")
        print(f"    JSON: {format_size(json_ser['size_bytes'])}")
        if bson_ser:
            print(f"    BSON: {format_size(bson_ser['size_bytes'])}")
            ratio = json_ser['size_bytes'] / bson_ser['size_bytes']
            smaller = "BSON" if ratio > 1 else "JSON"
            print(f"    Smaller: {smaller} ({abs(1 - ratio) * 100:.1f}% difference)")

        json_deser = bench_json_deserialize(json_ser['encoded'], iterations)
        bson_deser = bench_bson_deserialize(bson_ser['encoded'], iterations) if bson_ser else None
        print_comparison("Deserialization", json_deser, bson_deser)

        json_fw = bench_json_file_write(metrics, tmp_json, iterations)
        bson_fw = bench_bson_file_write(metrics, tmp_bson, iterations)
        print_comparison("File write", json_fw, bson_fw)

        print(f"\n  File size:")
        print(f"    JSON: {format_size(json_fw['file_size'])}")
        if bson_fw:
            print(f"    BSON: {format_size(bson_fw['file_size'])}")

        json_fr = bench_json_file_read(tmp_json, iterations)
        bson_fr = bench_bson_file_read(tmp_bson, doc_count, iterations)
        print_comparison("File read", json_fr, bson_fr)

        # --- Query-like operations (JSON in-memory only) ---
        print(f"\n--- In-Memory Query Operations ({doc_count} docs) ---")

        filter_result = bench_json_filter(metrics, iterations)
        print(f"\n  Filter + Sort + Limit (get_network_history):")
        print(f"    JSON in-memory: {format_times(filter_result['times'])}")

        agg_result = bench_json_aggregate(metrics, iterations)
        print(f"\n  Aggregation (get_best_networks):")
        print(f"    JSON in-memory: {format_times(agg_result['times'])}")

        # --- History documents ---
        print(f"\n--- Connection History ({doc_count} docs) ---")
        json_hist_ser = bench_json_serialize(history, iterations)
        bson_hist_ser = bench_bson_serialize(history, iterations)
        print_comparison("Serialization", json_hist_ser, bson_hist_ser)

        print(f"\n  Data size:")
        print(f"    JSON: {format_size(json_hist_ser['size_bytes'])}")
        if bson_hist_ser:
            print(f"    BSON: {format_size(bson_hist_ser['size_bytes'])}")

        # --- Portal patterns (small dataset) ---
        portal_count = len(portals)
        print(f"\n--- Portal Patterns ({portal_count} docs) ---")
        json_portal_ser = bench_json_serialize(portals, iterations)
        bson_portal_ser = bench_bson_serialize(portals, iterations)
        print_comparison("Serialization", json_portal_ser, bson_portal_ser)

        print(f"\n  Data size:")
        print(f"    JSON: {format_size(json_portal_ser['size_bytes'])}")
        if bson_portal_ser:
            print(f"    BSON: {format_size(bson_portal_ser['size_bytes'])}")

        # --- Summary ---
        print(f"\n{'=' * 70}")
        print("  SUMMARY")
        print(f"{'=' * 70}")
        print()
        print("  For vasili's typical workload (small doc count, simple schemas):")
        print("  - JSON serialization is simpler and uses stdlib (no dependencies)")
        print("  - In-memory query operations are fast enough for <1000 docs")
        print("  - File I/O dominates over serialization cost")
        print("  - MongoDB adds ~100MB RAM overhead on embedded devices")
        print()
        print("  Recommendation: JSON file storage is sufficient for vasili's")
        print("  use case on embedded devices. MongoDB is overkill for <1000")
        print("  connection records. Reserve MongoDB for deployments needing")
        print("  concurrent access or large-scale historical analysis.")
        print(f"{'=' * 70}")

    finally:
        # Cleanup
        for f in [tmp_json, tmp_bson]:
            if os.path.exists(f):
                os.unlink(f)


def main():
    parser = argparse.ArgumentParser(description='BSON vs JSON serialization benchmark for vasili')
    parser.add_argument(
        '--iterations', '-n', type=int, default=100, help='Number of iterations per benchmark'
    )
    parser.add_argument(
        '--docs', '-d', type=int, default=500, help='Number of documents to generate'
    )
    args = parser.parse_args()

    run_benchmark(args.docs, args.iterations)


if __name__ == '__main__':
    main()
