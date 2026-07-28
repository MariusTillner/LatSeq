#!/usr/bin/python3

import logging, argparse, sys, os, multiprocessing, bisect, json
from decimal import Decimal
from pathlib import Path
from tqdm import tqdm
from collections import defaultdict
from time import perf_counter
from dataclasses import dataclass, field
from concurrent.futures import ProcessPoolExecutor, as_completed

try:
    import orjson
    parse_json = orjson.loads
except ImportError:
    parse_json = json.loads


logging.basicConfig(
    level=logging.INFO,
    datefmt='%H:%M:%S',
    format='[%(asctime)s] [%(levelname)s] [%(funcName)s] %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

DURATION_TO_SEARCH_PKT_NS = 800_000_000  

KWS_NO_SEGMENTATION = {
    # Downlink
    ('sdap.pdu', 'pdcp.hdr'),
    ('pdcp.hdr', 'pdcp.int_ciph'),
    ('pdcp.int_ciph', 'rlc.buffer'),
    ('rlc.seg', 'rlc.tpoll_exp'),
    ('rlc.tpoll_exp', 'rlc.retx'),
    ('rlc.seg', 'rlc.nack'),
    ('rlc.nack', 'rlc.retx'),
    ('rlc.retx', 'mac.handover'),
    ('rlc.seg', 'mac.handover'),
    ('mac.handover', 'mac.subhdr'),
    ('mac.TB_assembled', 'mac.retx'),
    ('mac.retx', 'mac.dci'),
    ('mac.TB_assembled', 'mac.dci'),
    ('mac.dci', 'phy.crc'),
    ('phy.crc', 'phy.CB_seg'),
    ('phy.CB_seg', 'phy.ldpc'),
    ('phy.ldpc', 'phy.scrambled'),
    ('phy.scrambled', 'phy.modulated'),
    ('phy.modulated', 'phy.re_mapped'),
    ('phy.re_mapped', 'phy.rotated'),
    ('phy.rotated', 'phy.ifft'),
    ('phy.ifft', 'phy.tx_samples_out'),
    ('phy.tx_samples_out', 'phy.out'),
    # Uplink
    ('phy.rx_samples_start', 'phy.rx_samples_in'),
    ('phy.rx_samples_in', 'phy.fft'),
    ('phy.fft', 'phy.prach_pucch'),
    ('phy.prach_pucch', 'phy.CH_est'),
    ('phy.CH_est', 'phy.demodulated'),
    ('phy.CB_dec', 'phy.dec_fail'),
    ('phy.dec_fail', 'phy.prach_pucch'),
    ('phy.CB_dec', 'phy.TB_dec'),
    ('phy.TB_dec', 'phy.srs'),
    ('phy.srs', 'phy.rach_uci'),
    ('mac.demuxed', 'rlc.dec'),
    ('rlc.dec', 'rlc.reassembled'),
    ('rlc.reassembled', 'pdcp.hdr_dec'),
    ('pdcp.hdr_dec', 'pdcp.int_ciph_dec'),
    ('pdcp.int_ciph_dec', 'pdcp.deliver'),
    ('pdcp.deliver', 'sdap.sdu'),
    ('pdcp.deliver', 'pdcp.deliver_ooo'),
    ('pdcp.deliver_ooo', 'sdap.sdu'),
    ('pdcp.deliver', 'pdcp.deliver_reorder'),
    ('pdcp.deliver_reorder', 'sdap.sdu')
}

KWS_IN_D = {'sdap.pdu'}
KWS_OUT_D = {'phy.out'}
KWS_IN_U = {'phy.SOUTHstart', 'phy.rx_samples_start'}
KWS_OUT_U = {'gtp.out', 'phy.retx_drop', 'pdcp.discard_rcvdsmallerdeliv'}
VERBOSITY = True

def _nested_defaultdict_factory():
    return defaultdict(list)

@dataclass(slots=True, eq=False)
class Event:
    line_num: int
    ts: int
    direction: str | None
    src: str | None
    dest: str | None
    properties: dict = field(default_factory=dict)
    globalIDs: dict = field(default_factory=dict)
    localIDs: dict = field(default_factory=dict)


def _parse_chunk_worker(file_path_str: str, start_byte: int, end_byte: int, chunk_idx: int):
    events, lines_counted = [], 0
    with open(file_path_str, 'rb') as f:
        if start_byte != 0:
            f.seek(start_byte)
            f.readline()
        while (end_byte == -1 or f.tell() < end_byte) and (line_bytes := f.readline()):
            lines_counted += 1
            if not (line_str := line_bytes.decode('utf-8', errors='ignore').strip()):
                continue
            try:
                raw_data = parse_json(line_str)
            except Exception:
                continue

            props = raw_data.get('properties', {})
            try:
                if 'mac_sdu_sz' in props and float(props['mac_sdu_sz']) <= 3:
                    continue
            except (ValueError, TypeError):
                pass

            if (ts_val := raw_data.get('ts')) is None:
                continue

            events.append(Event(
                line_num=lines_counted,
                ts=int(Decimal(str(ts_val)) * 1_000_000_000),
                direction=raw_data.get('dir'),
                src=raw_data.get('src'),
                dest=raw_data.get('dest'),
                properties=props,
                globalIDs=raw_data.get('globalIDs', {}),
                localIDs=raw_data.get('localIDs', {})
            ))
    return chunk_idx, lines_counted, events


_SHARED_REBUILDER = None

def _rebuild_batch_worker(startpoint_batch):
    global _SHARED_REBUILDER
    batch_journeys = []
    for start_event in startpoint_batch:
        try:
            t0 = perf_counter()
            journeys = _SHARED_REBUILDER._rebuild_journeys_from_startpoint(start_event)
            rebuild_ms = 1000 * (perf_counter() - t0)
            for j in journeys:
                j['rebuild_time_ms'] = rebuild_ms
                if j.get('completed'):
                    batch_journeys.append(j)
        except Exception:
            continue
    return batch_journeys


class LatSeqLogParser:
    def __init__(self, filepath: str):
        logger.info(f"Initialize {self.__class__.__name__}")
        self.filepath = Path(filepath)
        self.startpoints: list[Event] = []
        self.all_events: list[Event] = []
        self.uplink_events_by_src, self.downlink_events_by_src = {}, {}
        self.local_id_index = defaultdict(_nested_defaultdict_factory)
        self.event_count = 0
        self._parse_in_parallel()
        logger.info(f"Parsed {self.event_count} events in {self.__class__.__name__}")

    def _parse_in_parallel(self):
        fsize = self.filepath.stat().st_size
        n_workers = min(os.cpu_count() or 4, max(1, fsize // (2 * 1024 * 1024)))
        csz = fsize // n_workers
        chunks = [(str(self.filepath), i * csz, -1 if i == n_workers - 1 else (i + 1) * csz, i) for i in range(n_workers)]

        logger.info(f"Parsing log in parallel using {n_workers} processes across {len(chunks)} chunks")
        
        results = []
        total_lines_read = 0

        with ProcessPoolExecutor(max_workers=n_workers) as executor:
            futures = [executor.submit(_parse_chunk_worker, *c) for c in chunks]
            
            with tqdm(total=len(chunks), desc="Parsing log files", unit="chunk", disable=not VERBOSITY) as pbar:
                for future in as_completed(futures):
                    res = future.result()
                    results.append(res)
                    total_lines_read += res[1]
                    pbar.set_postfix({"lines_parsed": f"{total_lines_read:,}"})
                    pbar.update(1)

        results.sort(key=lambda r: r[0])

        logger.info(f"Finished parsing {total_lines_read:,} total log lines across {len(results)} chunks. Building indices...")
        
        current_line_offset, up_by_src, down_by_src = 0, defaultdict(list), defaultdict(list)
        for _, lines_in_chunk, events in results:
            for ev in events:
                ev.line_num += current_line_offset
                self.event_count += 1
                self.all_events.append(ev)

                if ev.src and ev.localIDs:
                    for k, v in ev.localIDs.items():
                        try:
                            self.local_id_index[ev.src][(k, tuple(v) if isinstance(v, list) else v)].append(ev)
                        except TypeError:
                            pass

                if ev.src in KWS_IN_D or ev.src in KWS_IN_U:
                    self.startpoints.append(ev)
                elif ev.direction == 'U' and ev.src not in KWS_IN_U:
                    up_by_src[ev.src].append(ev)
                elif ev.direction == 'D' and ev.src not in KWS_IN_D:
                    down_by_src[ev.src].append(ev)
            current_line_offset += lines_in_chunk

        self.uplink_events_by_src = {k: {"events": (evs := sorted(v, key=lambda e: e.ts)), "timestamps": [e.ts for e in evs]} for k, v in up_by_src.items()}
        self.downlink_events_by_src = {k: {"events": (evs := sorted(v, key=lambda e: e.ts)), "timestamps": [e.ts for e in evs]} for k, v in down_by_src.items()}

    def analyze_matching_local_ids(self):
        """Analyzes 2-hop transitions (A -> B ==> B -> C) to map localIDs used for matching across hops."""
        hop_keys = defaultdict(set)
        hop_directions = defaultdict(set)
        hop_counts = defaultdict(int)

        # 1. Collect unique localID keys for every single hop (src -> dest)
        for ev in self.all_events:
            if ev.src and ev.dest:
                pair = (ev.src, ev.dest)
                hop_counts[pair] += 1
                if ev.direction:
                    hop_directions[pair].add(ev.direction)
                if ev.localIDs:
                    hop_keys[pair].update(ev.localIDs.keys())

        # 2. Map 2-hop transitions: (A -> B) followed by (B -> C)
        two_hop_transitions = []
        all_hops = list(hop_keys.keys())

        for (src1, dest1) in all_hops:
            for (src2, dest2) in all_hops:
                if dest1 == src2:  # Found node B transition point
                    in_pair = (src1, dest1)
                    out_pair = (src2, dest2)
                    
                    in_keys = hop_keys[in_pair]
                    out_keys = hop_keys[out_pair]
                    
                    # Intersect incoming and outgoing localID keys
                    matching_keys = in_keys & out_keys

                    dirs = hop_directions[in_pair] | hop_directions[out_pair]
                    if 'D' in dirs or src1 in KWS_IN_D or dest2 in KWS_OUT_D:
                        direction = 'Downlink (D)'
                    elif 'U' in dirs or src1 in KWS_IN_U or dest2 in KWS_OUT_U:
                        direction = 'Uplink (U)'
                    else:
                        direction = 'Unknown / Unassigned'

                    two_hop_transitions.append({
                        'in_pair': in_pair,
                        'out_pair': out_pair,
                        'point': dest1,
                        'direction': direction,
                        'in_keys': sorted(list(in_keys)),
                        'out_keys': sorted(list(out_keys)),
                        'matching_keys': sorted(list(matching_keys)),
                        'no_seg_in': in_pair in KWS_NO_SEGMENTATION,
                        'no_seg_out': out_pair in KWS_NO_SEGMENTATION,
                    })

        two_hop_transitions.sort(key=lambda x: (x['direction'], x['point'], x['in_pair'], x['out_pair']))

        downlink_hops = [t for t in two_hop_transitions if 'Downlink' in t['direction']]
        uplink_hops = [t for t in two_hop_transitions if 'Uplink' in t['direction']]
        other_hops = [t for t in two_hop_transitions if 'Downlink' not in t['direction'] and 'Uplink' not in t['direction']]

        print("\n" + "=" * 95)
        print(" LATSEQ 2-HOP TRANSITION & MATCHING LOCAL-ID MAPPING (A -> B  ==>  B -> C)")
        print("=" * 95)

        def _print_section(title, transitions):
            print(f"\n=== {title} ({len(transitions)} transition branches) " + "=" * (55 - len(title)))
            if not transitions:
                print("  (No transitions found)")
                return
            
            curr_point = None
            for t in transitions:
                if t['point'] != curr_point:
                    curr_point = t['point']
                    print(f"\n  [ Intermediate Node: '{curr_point}' ]")
                    print("  " + "-" * 88)

                in_str = f"('{t['in_pair'][0]}', '{t['in_pair'][1]}')"
                out_str = f"('{t['out_pair'][0]}', '{t['out_pair'][1]}')"
                
                print(f"  Transition Path   : {in_str}  ==>  {out_str}")
                print(f"    Incoming Keys   : {t['in_keys']}")
                print(f"    Outgoing Keys   : {t['out_keys']}")
                print(f"    MATCHED KEYS    : {t['matching_keys']}  <-- Shared keys used by rebuilder")
                print(f"    No-Segmentation : In: {t['no_seg_in']} | Out: {t['no_seg_out']}")
                print()

        _print_section("DOWNLINK TRANSITIONS", downlink_hops)
        _print_section("UPLINK TRANSITIONS", uplink_hops)
        if other_hops:
            _print_section("OTHER TRANSITIONS", other_hops)

        print("=" * 95 + "\n")


class LatSeqJourneyRebuilder:
    def __init__(self, latseq_log_parser: LatSeqLogParser, output_file_path: str | None = None, write_to_stdout: bool = False):
        logger.info(f"Initialize {self.__class__.__name__}")
        self.startpoints = latseq_log_parser.startpoints
        self.uplink_events_by_src = latseq_log_parser.uplink_events_by_src
        self.downlink_events_by_src = latseq_log_parser.downlink_events_by_src
        self.local_id_index = latseq_log_parser.local_id_index
        self.output_file_path = output_file_path
        self.write_to_file = bool(output_file_path)
        self.write_to_stdout = write_to_stdout
        self.journeys: list[dict] = []
        logger.info(f"Initialized {self.__class__.__name__} with {len(self.startpoints)} startpoints")

    def rebuild_journeys(self, batch_size: int = 500) -> None:
        global _SHARED_REBUILDER
        _SHARED_REBUILDER = self
        total = len(self.startpoints)
        logger.info(f"Starting parallel journey rebuilding from {total} startpoints")

        batches = [self.startpoints[i:i + batch_size] for i in range(0, total, batch_size)]
        n_workers = min(os.cpu_count() or 4, max(1, len(batches)))
        local_journeys = []

        with ProcessPoolExecutor(max_workers=n_workers) as executor:
            futures = [executor.submit(_rebuild_batch_worker, b) for b in batches]
            with tqdm(total=total, desc="Rebuilding journeys", unit="startpoints", leave=True, disable=not VERBOSITY) as pbar:
                for future in as_completed(futures):
                    local_journeys.extend(future.result())
                    pbar.update(batch_size)

        print(file=sys.stderr)
        logger.info(f"Journeys rebuilt: {len(local_journeys)}")
        self.journeys = self._finalize_journeys(local_journeys)
        logger.info("Journeys finalized and packet IDs assigned.")

    def _rebuild_journeys_from_startpoint(self, start_event: Event) -> list[dict]:
        journeys = [{'completed': False, 'stuck': False, 'dir': start_event.direction, 'events': [start_event]}]
        while not all(j['completed'] or j['stuck'] for j in journeys):
            for idx, j in enumerate(journeys):
                if j['completed'] or j['stuck']:
                    continue

                matches = self._find_matching_events_for_journey(j)

                # --- DIAGNOSTIC PRINT ---
                if len(matches) > 5:  # High branching threshold
                    last_ev = j['events'][-1]
                    logger.warning(
                        f"Explosion detected at node '{last_ev.dest}'! "
                        f"Found {len(matches)} matching events for key {last_ev.localIDs}"
                    )
                # ------------------------

                if not matches:
                    j['stuck'] = True
                elif len(matches) > 1:
                    self._branch_journey_for_multiple_matches(journeys, idx, matches)
                else:
                    self._extend_journey_with_event(j, matches[0])
        return journeys

    def _find_matching_events_for_journey(self, journey: dict) -> list[Event]:
        last_ev = journey['events'][-1]
        if not (src_idx := self.local_id_index.get(last_ev.dest)) or not (prev_ids := last_ev.localIDs):
            return []

        prev_ts, max_ts = last_ev.ts, last_ev.ts + DURATION_TO_SEARCH_PKT_NS
        no_seg = (last_ev.src, last_ev.dest) in KWS_NO_SEGMENTATION
        best_candidates = None

        for k, v in prev_ids.items():
            if not (ev_list := src_idx.get((k, tuple(v) if isinstance(v, list) else v))):
                continue
            left = bisect.bisect_left(ev_list, prev_ts, key=lambda e: e.ts)
            right = bisect.bisect_right(ev_list, max_ts, key=lambda e: e.ts)
            if (count := right - left) == 0:
                return []
            if best_candidates is None or count < len(best_candidates):
                best_candidates = ev_list[left:right]

        if not best_candidates:
            return []

        matched = []
        for ev in best_candidates:
            shared_found, match_valid = False, True
            for k, v in prev_ids.items():
                if (val := ev.localIDs.get(k)) is not None:
                    shared_found = True
                    if val != v:
                        match_valid = False; break
            if shared_found and match_valid:
                matched.append(ev)
                if no_seg:
                    return matched
        return matched

    def _branch_journey_for_multiple_matches(self, journeys: list[dict], base_idx: int, matches: list[Event]) -> None:
        base = journeys[base_idx]
        branches = [base] + [{**base, 'events': base['events'].copy()} for _ in range(len(matches) - 1)]
        for j, match in zip(branches, matches):
            self._extend_journey_with_event(j, match)
        journeys.extend(branches[1:])

    def _extend_journey_with_event(self, journey: dict, event: Event) -> None:
        journey['events'].append(event)
        if event.dest in KWS_OUT_U or event.dest in KWS_OUT_D:
            journey['completed'] = True

    def _compute_journey_metadata(self, journey: dict) -> None:
        journey['events'].sort(key=lambda e: e.ts)
        for k in ('stuck', 'completed'):
            journey.pop(k, None)
        journey['ts_in'] = (ts_in := journey['events'][0].ts / 1e9)
        journey['ts_out'] = (ts_out := journey['events'][-1].ts / 1e9)
        journey['latency'] = (lat := ts_out - ts_in)
        journey['latency_ms'] = 1000 * lat
        for p in ('properties', 'globalIDs', 'localIDs'):
            journey[p] = {}
        for ev in journey['events']:
            self._update_collect(journey['properties'], ev.properties)
            self._update_collect(journey['globalIDs'], ev.globalIDs)
            self._update_collect(journey['localIDs'], ev.localIDs)

    def _update_collect(self, exist: dict, new: dict) -> None:
        for k, v in new.items():
            if k not in exist:
                exist[k] = v
            elif isinstance(exist[k], list):
                if v not in exist[k]:
                    exist[k].append(v)
            elif v != exist[k]:
                exist[k] = [exist[k], v]

    def _assign_packet_ids(self, journeys):
        pmap, pid = {}, 0
        for j in journeys:
            evs = j.get('events')
            if j.get('dir') not in ('U', 'D') or not evs:
                j['packet_id'] = None; continue
            key = evs[0].line_num if j['dir'] == 'D' else evs[-1].line_num
            if key not in pmap:
                pmap[key] = pid; pid += 1
            j['packet_id'] = pmap[key]
        return journeys

    def _finalize_journeys(self, journeys):
        for j in journeys:
            self._compute_journey_metadata(j)
        journeys = self._assign_packet_ids(journeys)
        for jid, j in enumerate(journeys):
            j['journey_id'] = jid

        order = ["dir", "packet_id", "journey_id", "latency", "latency_ms", "ts_in", "ts_out", "rebuild_time_ms", "localIDs", "globalIDs", "properties"]
        for j in journeys:
            j.pop('globalIDs', None)
            j['events'] = [{'line_num': e.line_num, 'ts': e.ts / 1e9, 'src': e.src, 'dest': e.dest, 'properties': e.properties, 'localIDs': e.localIDs} for e in j.get('events', [])]
        return [{k: j[k] for k in order + list(j.keys()) if k in j} for j in journeys]

    def journeys_to_json(self, output_file_path=None):
        if not self.journeys:
            self.rebuild_journeys()
        path = output_file_path or self.output_file_path
        logger.info("Serializing journeys to JSON Lines")

        if self.write_to_file and path:
            logger.info(f"Writing journeys to file: {path}")
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(j) + "\n" for j in self.journeys)
            logger.info(f"Finished writing journeys to {path}")

        if self.write_to_stdout:
            logger.info("Writing journeys to stdout")
            for j in self.journeys:
                print(json.dumps(j))
            logger.info("Finished writing journeys to stdout")


def main():
    multiprocessing.set_start_method('fork', force=True)
    parser = argparse.ArgumentParser(description="Reconstruct individual packet traces and calculate latency from latseq log files.")
    parser.add_argument("-l", "--log-file", required=True, help="Path to the input latseq log file (e.g., unix_time.lseq).")
    parser.add_argument("-o", "--output-file-path", help="Optional path to write journeys as JSON.")
    parser.add_argument("--stdout", action="store_true", help="Enable printing journeys to stdout.")
    parser.add_argument("-j", "--journeys", action="store_true", help="Convert parsed journeys to JSON (calls journeys_to_json()).")
    parser.add_argument("-m", "--map-local-ids", action="store_true", help="Analyze and display 2-hop localIDs used for matching without rebuilding journeys.")
    args = parser.parse_args()

    processor = LatSeqLogParser(args.log_file)

    if args.map_local_ids:
        processor.analyze_matching_local_ids()
        return

    rebuilder = LatSeqJourneyRebuilder(processor, output_file_path=args.output_file_path, write_to_stdout=args.stdout)
    if args.journeys:
        rebuilder.journeys_to_json()


if __name__ == "__main__":
    main()