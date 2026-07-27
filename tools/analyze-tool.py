#!/usr/bin/env python3

import argparse
import json
import math
import operator
import os
import resource
import statistics
import sys
from collections import defaultdict

# Map string operators to Python functions
OPERATORS = {
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
}


def set_memory_governor(max_gb=12.0):
    """Restricts the process from consuming more than a specified amount of RAM."""
    max_bytes = int(max_gb * 1024 * 1024 * 1024)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))
        print(f"🔒 Memory governor active: Script will abort if RAM exceeds {max_gb} GB")
    except Exception as e:
        print(f"⚠️ Memory governor could not be initialized ({e})")


class JourneyStream:
    def __init__(self, file_path=None):
        self.file_path = file_path
        self.total_lines = self._quick_count_lines() if file_path else 0
        
        # Storing pipe lines in memory ONLY if we are reading from stdin 
        # because stdin cannot be re-read multiple times natively.
        self.stdin_cache = None
        if not self.file_path:
            print("📥 Reading standard input pipeline (stdin) into active memory cache...")
            self.stdin_cache = [line for line in sys.stdin if line.strip()]
            print(f"Cached {len(self.stdin_cache):,} pipeline lines.", file=sys.stderr)
            
        # Base input generator reference
        self._active_source = self._base_stream

    def _quick_count_lines(self):
        if not self.file_path or not os.path.exists(self.file_path):
            return 0
        print(f"Counting total lines in {self.file_path}...")
        lines = 0
        try:
            with open(self.file_path, "rb") as f:
                buf_size = 1024 * 1024
                read_bytes = f.read(buf_size)
                while read_bytes:
                    lines += read_bytes.count(b"\n")
                    read_bytes = f.read(buf_size)
            print(f"Total lines found: {lines:,}")
            return lines
        except Exception as e:
            print(f"Could not pre-count lines ({e}). Progress percentage disabled.")
            return 0

    def _base_stream(self):
        """Underlying core stream iterator."""
        if self.stdin_cache is not None:
            for line in self.stdin_cache:
                yield json.loads(line)
        else:
            lines_read = 0
            with open(self.file_path, "r", encoding="utf-8") as infile:
                for line in infile:
                    if line.strip():
                        lines_read += 1
                        if lines_read % 10000 == 0:
                            if self.total_lines > 0:
                                percentage = (lines_read / self.total_lines) * 100
                                print(f" -> Processing {lines_read:,} lines ({percentage:.2f}%) ...", end="\r", file=sys.stderr)
                            else:
                                print(f" -> Processing {lines_read:,} lines ...", end="\r", file=sys.stderr)
                        yield json.loads(line)
            print(f"\nFinished processing stream layer pass. Checked lines: {lines_read:,}", file=sys.stderr)

    def get_fresh_stream(self):
        """Generates an evaluation stream that honors any attached pipeline chain filters."""
        return self._active_source()

    def _clone_with_source(self, new_source):
        """Spawns a clean stream context copy sharing the underlying file or stdin cache pointer."""
        clone = JourneyStream.__new__(JourneyStream)
        clone.file_path = self.file_path
        clone.total_lines = self.total_lines
        clone.stdin_cache = self.stdin_cache  # Shares the pipeline memory buffer pool natively!
        clone._active_source = new_source
        return clone

    # --- FLUENT PIPELINE INTERMEDIATE FILTERS ---

    def filter_by_localid(self, localid, comp_str, value):
        op_func = OPERATORS.get(comp_str)
        if not op_func:
            raise ValueError(f"Unsupported operator: {comp_str}")

        upstream_source = self._active_source
        def generator_filter():
            for journey in upstream_source():
                local_ids = journey.get("localIDs", {})
                if localid in local_ids and op_func(local_ids[localid], value):
                    yield journey
        
        return self._clone_with_source(generator_filter)

    def filter_by_root(self, key, comp_str, value):
        print(f"Applying root-level filter: {key} {comp_str} {value}...")
        op_func = OPERATORS.get(comp_str)
        if not op_func:
            raise ValueError(f"Unsupported operator: {comp_str}")

        upstream_source = self._active_source
        def generator_filter():
            for journey in upstream_source():
                if key in journey and op_func(journey[key], value):
                    yield journey
        
        return self._clone_with_source(generator_filter)

    def filter_by_event_src(self, src_name):
        print(f"Applying event source filter: {src_name}...")
        upstream_source = self._active_source
        def generator_filter():
            for journey in upstream_source():
                if any(event.get("src") == src_name for event in journey.get("events", [])):
                    yield journey
        
        return self._clone_with_source(generator_filter)

    def filter_worst_journey_per_packet(self):
        print("Grouping journeys by Packet ID to isolate worst latency segments...")
        """Groups upstream states and isolates the individual worst-performing variations."""
        upstream_source = self._active_source
        
        def worst_journey_generator():
            packet_aggregator = defaultdict(list)
            
            for journey in upstream_source():
                p_id = journey.get("packet_id")
                if p_id is not None:
                    packet_aggregator[p_id].append(journey)
                else:
                    packet_aggregator[f"orphan_{id(journey)}"].append(journey)
            
            for p_id, journeys in packet_aggregator.items():
                worst_journey = max(journeys, key=lambda j: j.get("latency_ms", 0.0))
                yield worst_journey

        return self._clone_with_source(worst_journey_generator)

    # --- TERMINAL METHODS & ANALYTICS FEATURES ---

    def fingerprint_layers(self):
        print("Analyzing architectural layer processing footprints")
        layer_times = defaultdict(float)
        total_accumulated_ms = 0.0

        for journey in self.get_fresh_stream():
            events = journey.get("events", [])
            for i in range(len(events) - 1):
                delta_ms = (events[i+1].get("ts", 0) - events[i].get("ts", 0)) * 1000.0
                if delta_ms >= 0:
                    src = events[i].get("src", "UNKNOWN")
                    layer_name = src.split('.')[0].upper()
                    layer_times[layer_name] += delta_ms
                    total_accumulated_ms += delta_ms

        if not layer_times:
            print("No event layer timing metrics found.")
            return self

        print("\n" + "=" * 60)
        print(" 🧠 STRUCTURAL MICRO-BOTTLENECK LAYER FINGERPRINT")
        print("=" * 60)
        print(f" Total Monitored Runtime: {total_accumulated_ms:.2f} ms\n")
        print(f" {'LAYER':<12} | {'CUMULATIVE TIME':<18} | {'INFLUENCE SHARE'}")
        print("-" * 60)
         
        for layer, duration in sorted(layer_times.items(), key=lambda x: x[1], reverse=True):
            pct = (duration / total_accumulated_ms) * 100 if total_accumulated_ms > 0 else 0
            bar = "█" * int(pct // 4)
            print(f" [{layer:<8}]   | {duration:<14.2f} ms | {pct:6.2f}%  {bar}")
        print("=" * 60 + "\n")
        return self

    def analyze_assembly_tax(self):
        print("Evaluating packet fragmentation delays and assembly tax")
        packet_timestamps = defaultdict(list)

        for journey in self.get_fresh_stream():
            p_id = journey.get("packet_id")
            events = journey.get("events", [])
            if p_id is not None and events:
                timestamps = [e.get("ts", 0) for e in events]
                packet_timestamps[p_id].extend(timestamps)

        assembly_spans_ms = []
        for p_id, t_list in packet_timestamps.items():
            if len(t_list) > 1:
                delta_span_ms = (max(t_list) - min(t_list)) * 1000.0
                assembly_spans_ms.append(delta_span_ms)

        if not assembly_spans_ms:
            print("❌ No multi-fragment or valid packet groups found to calculate tax.")
            return self

        assembly_spans_ms.sort()
        print("\n" + "=" * 50)
        print(" 📦 PACKET FRAGMENTATION & ASSEMBLY TAX REPORT")
        print("=" * 50)
        print(f" Packets Evaluated:     {len(assembly_spans_ms):,}")
        print(f" Min Assembly Delta:    {assembly_spans_ms[0]:.4f} ms")
        print(f" Max Assembly Delta:    {assembly_spans_ms[-1]:.4f} ms")
        print(f" Mean Assembly Tax:     {statistics.mean(assembly_spans_ms):.4f} ms")
        print("-" * 50)
        
        percentiles = [50, 90, 95, 99]
        for p in percentiles:
            idx = int((p / 100) * (len(assembly_spans_ms) - 1))
            print(f" Assembly Tail p{p:<2}:     {assembly_spans_ms[idx]:.4f} ms")
        print("=" * 50 + "\n")
        return self

    def print_journeys(self, target_ids=None, max_samples=3, trim_io=False):
        if target_ids:
            target_set = set(str(tid) for tid in target_ids)
            found_set = set()
         
        printed_count = 0
        for journey in self.get_fresh_stream():
            j_id = str(journey.get("journey_id"))
            if target_ids and j_id not in target_set:
                 continue
 
            p_id = journey.get("packet_id", "N/A")
            original_latency = journey.get("latency_ms", 0.0)
            direction = str(journey.get("dir", "N/A")).upper()
            events = journey.get("events", [])

            if len(events) < 2:
                continue

            hops = []
            for i in range(len(events) - 1):
                ev_curr = events[i]
                ev_next = events[i+1]
                delta_ms = (ev_next.get("ts", 0) - ev_curr.get("ts", 0)) * 1000.0
                
                src_name = ev_curr.get("src", "unknown")
                dest_name = ev_next.get("src", "unknown")
                layer = f"[{src_name.split('.')[0].upper():<5}]"
                hop_string = f"{src_name} -> {dest_name}"
                
                hops.append({
                    "layer": layer,
                    "hop_string": hop_string,
                    "delta_ms": delta_ms
                })

            adjusted_latency = original_latency
            is_trimmed = False
            
            if trim_io and hops:
                if direction == "U":
                    removed = hops.pop(0)
                    adjusted_latency -= removed["delta_ms"]
                    is_trimmed = True
                elif direction == "D":
                    removed = hops.pop(-1)
                    adjusted_latency -= removed["delta_ms"]
                    is_trimmed = True

            adjusted_latency = max(adjusted_latency, 0.0001)

            print("=" * 90)
            trim_status = " [I/O BOUNDARIES TRIMMED]" if is_trimmed else ""
            print(f" 🚀 JOURNEY TRACE | ID: {j_id} | PACKET ID: {p_id} | DIR: {direction}{trim_status}")
            print("=" * 90)
            if is_trimmed:
                print(f" Core Compute Latency: {adjusted_latency:.4f} ms (Original Line Latency: {original_latency:.4f} ms)")
            else:
                print(f" Overall Fragment Latency: {original_latency:.4f} ms")
            
            local_ids = journey.get("localIDs", {})
            properties = journey.get("properties", {})
            
            if local_ids or properties:
                print("-" * 90)
                if local_ids:
                    id_str = ", ".join(f"{k}: {v}" for k, v in local_ids.items())
                    print(f"  🏷️  Local IDs  : {id_str}")
                if properties:
                    prop_str = ", ".join(f"{k}: {v}" for k, v in properties.items())
                    print(f"  📊 Properties : {prop_str}")

            print("-" * 90)

            for hop in hops:
                delta_ms = hop["delta_ms"]
                display_time = f"{delta_ms * 1000.0:+8.3f} us" if delta_ms < 1.0 else f"{delta_ms:+8.3f} ms"
                pct = int((delta_ms / adjusted_latency) * 100) if adjusted_latency > 0 else 0
                pct = min(max(pct, 0), 100)
                bar = "█" * (pct // 5)
                print(f"  {hop['layer']} {hop['hop_string']:<55} -> [{display_time}]  {bar:<20} ({pct:3}%)")
             
            print("=" * 90 + "\n")
            printed_count += 1
            
            if target_ids:
                found_set.add(j_id)
                if found_set == target_set:
                    print("✅ Found all target IDs early. Terminating stream trace pass.", file=sys.stderr)
                    break
            elif printed_count >= max_samples:
                break
        
        if target_ids and len(found_set) == 0:
            print("❌ No matching journey records found for the specified target IDs.")
        return self

    def statistics(self, target_key, percentiles=None, is_localid=False):
        if percentiles is None:
            percentiles = [50, 90, 95, 99]
             
        print(f"Statistical analysis on '{target_key}'...")
        values = []

        for journey in self.get_fresh_stream():
            if is_localid:
                val = journey.get("localIDs", {}).get(target_key)
            else:
                val = journey.get(target_key)
                
            if val is not None and isinstance(val, (int, float)):
                values.append(val)

        if not values:
            print("No matching numeric data found to compute statistics.")
            return {}

        values.sort()
        n = len(values)

        stats = {
            "count": n,
            "min": values[0],
            "max": values[-1],
            "mean": statistics.mean(values),
        }

        stats["percentiles"] = {}
        for p in percentiles:
            if p == 0:
                stats["percentiles"][f"p{p}"] = values[0]
            elif p == 100:
                stats["percentiles"][f"p{p}"] = values[-1]
            else:
                k = (n - 1) * (p / 100)
                f = math.floor(k)
                c = math.ceil(k)
                if f == c:
                    stats["percentiles"][f"p{p}"] = values[int(k)]
                else:
                    stats["percentiles"][f"p{p}"] = values[f] + (k - f) * (values[c] - values[f])

        print("\n" + "="*40)
        print(f" STATISTICAL REPORT FOR: {target_key}")
        print("="*40)
        print(f" Sample Count:  {stats['count']:,}")
        print(f" Minimum:       {stats['min']:.2f}")
        print(f" Maximum:       {stats['max']:.2f}")
        print(f" Mean (Avg):    {stats['mean']:.2f}")
        print("-" * 40)
        for pct, val in stats["percentiles"].items():
            print(f" Percentile {pct.upper()}: {val:.2f}")
        print("="*40 + "\n")

        return stats

    def save(self, output_path):
        print(f"Starting pipeline processing...")
        match_count = 0
        with open(output_path, "w", encoding="utf-8") as outfile:
            for journey in self.get_fresh_stream():
                outfile.write(json.dumps(journey) + "\n")
                match_count += 1
        print(f"Done!\n └─ Saved lines: {match_count:,}")


def main():
    set_memory_governor(max_gb=12.0)

    parser = argparse.ArgumentParser(description="Performance Journey Stream Processing Engine")
    parser.add_argument("-i", "--input", help="Path to input jsonl file. If absent, reads from stdin.")
    parser.add_argument("-j", "--print-journeys", nargs="+", help="List of specific journey IDs to trace and print.")
    parser.add_argument("--trim-io", action="store_true", help="Trim real-time IQ rx accumulation (UL first hop) and async TX flushing buffers (DL last hop).")
    args = parser.parse_args()

    engine = JourneyStream(file_path=args.input)

    # Targeted diagnostic pass if matching keys are passed
    if args.print_journeys:
        engine.print_journeys(target_ids=args.print_journeys, trim_io=args.trim_io)
    else:
        print("\n\n")
        # Runs Downlink and Uplink reports sequentially on independent, side-effect-free cloned streams
        print("--- DOWNLINK PACKET PROCESSING ---")
        (
            engine
            .filter_worst_journey_per_packet()
            .filter_by_root("dir", "==", "D")
            .statistics("latency_ms", percentiles=[10, 20, 30, 40, 50, 90, 95, 99, 99.9, 99.99, 99.999])
        )
        (
            engine
            .filter_worst_journey_per_packet()
            .filter_by_root("dir", "==", "D")
            .filter_by_root("latency_ms", "<", 0.08)
            .print_journeys(max_samples=3, trim_io=True)
        )
        
        print("--- UPLINK PACKET PROCESSING ---")
        #(
        #    engine
        #    .filter_worst_journey_per_packet()
        #    .filter_by_root("dir", "==", "U")
        #    .statistics("latency_ms", percentiles=[10, 20, 30, 40, 50, 90, 95, 99, 99.9, 99.99, 99.999])
        #)
        (
            engine
            .filter_worst_journey_per_packet()
            .filter_by_root("dir", "==", "U")
            .filter_by_root("latency_ms", "<", 0.6)
            .print_journeys(max_samples=3, trim_io=True)
        )


if __name__ == "__main__":
    main()