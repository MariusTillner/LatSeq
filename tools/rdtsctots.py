#!/usr/bin/env python3

import sys
import json
import argparse
from pathlib import Path

class RdtscToTs:
    def __init__(self, filename):
        self.filename = filename
        self.parsed_lines = []
        self._load_cleanup_and_sort()

    def _load_cleanup_and_sort(self):
        """Loads and tokenizes the file in a single pass, then sorts immediately in memory."""
        sys.stderr.write(f"[+] Step 1: Loading and tokenizing rows from '{self.filename}'...\n")
        
        parsed = []
        with open(self.filename, 'r', encoding='utf-8') as f:
            for line in f:
                line_str = line.strip()
                if not line_str or line_str.startswith("#"):
                    continue
                
                parts = line_str.split()
                if len(parts) >= 4:
                    parsed.append(parts)
                    
        if not parsed:
            raise IOError(f"No valid data records found in {self.filename}")
            
        sys.stderr.write(f"    -> Successfully loaded {len(parsed)} data records.\n")
        sys.stderr.write("[+] Step 2: Sorting records chronologically by RDTSC ticks...\n")
        
        # In-place sort using the already-isolated first column (ticks)
        parsed.sort(key=lambda x: int(x[0]))
        self.parsed_lines = parsed
        sys.stderr.write("    -> Sorting complete.\n")

    def _get_offset_cpufreq(self):
        """Calculates CPU frequency by scanning from boundaries to avoid full array allocations."""
        sys.stderr.write("[+] Step 3: Calculating CPU clock synchronization factors...\n")
        
        # Fast boundary scan for the first and last sync frames
        first_S = None
        for p in self.parsed_lines:
            if p[1] == 'S':
                first_S = p
                break
                
        last_S = None
        for p in reversed(self.parsed_lines):
            if p[1] == 'S':
                last_S = p
                break
        
        if not first_S or not last_S:
            sys.stderr.write("    -> Warning: Missing 'S' sync lines. Defaulting to 3.0 GHz baseline.\n")
            return int(self.parsed_lines[0][0]), 0.0, 3000000000
            
        cycle_offset = int(first_S[0])
        time_offset = float(first_S[3])
        
        cycles_delta = int(last_S[0]) - cycle_offset
        time_delta = float(last_S[3]) - time_offset
        
        if time_delta == 0:
            raise ZeroDivisionError("Sync intervals match perfectly; cannot compute CPU clock frequency.")
            
        cpufreq = int(cycles_delta / time_delta)
        sys.stderr.write(f"    -> Synchronized. Computed CPU Clock: {cpufreq / 1000000000:.4f} GHz\n")
        return cycle_offset, time_offset, cpufreq
    
    def parse_metadata_section(self, section_str):
        """Highly optimized key/val string parser."""
        if not section_str:
            return {}
            
        data = {}
        for chunk in section_str.split('.'):
            if not chunk:
                continue
            if '=' in chunk:
                k, v = chunk.split('=', 1)
                v_str = v.strip()
                try:
                    data[k.strip()] = int(v_str)
                except ValueError:
                    data[k.strip()] = v_str
            else:
                data[chunk.strip()] = True
        return data

    def stream_jsonl(self):
        """Converts raw clock ticks to Unix timestamps and outputs JSON strings at maximum speed."""
        cycle_offset, time_offset, cpufreq = self._get_offset_cpufreq()
        sys.stderr.write("[+] Step 4: Streaming and parsing log layout structures into JSONL...\n")
        
        total_to_process = len(self.parsed_lines)
        
        # Localize functions to eliminate dynamic attribute lookup overhead in the hot loop
        parse_meta = self.parse_metadata_section
        json_dumps = json.dumps
        write_err = sys.stderr.write
        flush_err = sys.stderr.flush
        
        for processed_count, parts in enumerate(self.parsed_lines, 1):
            direction = parts[1]
            if direction == 'S':
                continue
                
            rdtsc_ticks = int(parts[0])
            unix_ts = ((rdtsc_ticks - cycle_offset) / cpufreq) + time_offset
            
            src_dest = parts[2].split("--")
            src = src_dest[0] if src_dest else "unknown"
            dest = src_dest[1] if len(src_dest) > 1 else "unknown"
            
            # Fast positional slicing without padding loops
            sections = parts[3].split(':', 2)
            len_sections = len(sections)
            
            json_packet = {
                "dir": direction,
                "ts": round(unix_ts, 9),
                "src": src,
                "dest": dest,
                "properties": parse_meta(sections[0]) if len_sections > 0 else {},
                "globalIDs": parse_meta(sections[1]) if len_sections > 1 else {},
                "localIDs": parse_meta(sections[2]) if len_sections > 2 else {}
            }
            
            # Check progress less frequently (every 100k rows) to prevent terminal I/O lag
            if processed_count % 100000 == 0 or processed_count == total_to_process:
                pct = (processed_count / total_to_process) * 100
                write_err(f"\r    -> Parsing progress: {processed_count}/{total_to_process} records completed ({pct:.2f}%)")
                flush_err()
                
            yield json_dumps(json_packet)
            
        write_err("\n[+] Success: Parsing processing thread safely shut down.\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert raw RDTSC trace sequences (.lseq) into highly-optimized JSONL records."
    )
    parser.add_argument(
        "-i", "--input", 
        type=str, 
        required=True,
        help="Path to the raw telemetry log sequence input file (e.g. data.lseq)"
    )
    parser.add_argument(
        "-o", "--output", 
        type=str, 
        default=None,
        help="Path to output file (e.g. data.jsonl). If omitted, streams directly to stdout."
    )

    args = parser.parse_args()

    if not Path(args.input).exists():
        sys.stderr.write(f"[!] Error: Input file '{args.input}' does not exist.\n")
        sys.exit(1)

    try:
        converter = RdtscToTs(args.input)
        
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as out_f:
                for json_line in converter.stream_jsonl():
                    out_f.write(json_line + "\n")
            sys.stderr.write(f"[+] Output successfully compiled into '{args.output}'\n")
        else:
            for json_line in converter.stream_jsonl():
                sys.stdout.write(json_line + "\n")
                
    except Exception as e:
        sys.stderr.write(f"\n[!] Conversion terminated unexpectedly: {e}\n")
        sys.exit(1)