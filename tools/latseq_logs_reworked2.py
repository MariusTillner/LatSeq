#!/usr/bin/python3

import logging
import argparse
import sys
from decimal import Decimal
from pathlib import Path
from tqdm import tqdm
from collections import defaultdict
import bisect
from time import perf_counter
import json
from typing import Generator

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(funcName)s] %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

# Reducing search space
DURATION_TO_SEARCH_PKT = Decimal(0.8)

# CONSTANTS
S_TO_MS = 1000

# Converted lists to sets for O(1) membership testing instead of O(N)
KWS_NO_SEGMENTATION = {
    # Downlink
    'sdap.pdu--pdcp.hdr',
    'pdcp.hdr--pdcp.int_ciph',
    'pdcp.int_ciph--rlc.buffer',
    'rlc.seg--rlc.tpoll_exp',
    'rlc.tpoll_exp--rlc.retx',
    'rlc.seg--rlc.nack',
    'rlc.nack--rlc.retx',
    'rlc.retx--mac.handover',
    'rlc.seg--mac.handover',
    'mac.handover--mac.subhdr',
    'mac.subhdr--mac.retx',
    'mac.subhdr--mac.dci',
    'mac.dci--phy.crc',
    'phy.crc--phy.CB_seg',
    'phy.CB_seg--phy.ldpc',
    'phy.ldpc--phy.scrambled',
    'phy.scrambled--phy.modulated',
    'phy.modulated--phy.re_mapped',
    'phy.re_mapped--phy.rotated',
    'phy.rotated--phy.ifft',
    'phy.ifft--phy.tx_sample_out',
    'phy.ifft--phy.tx_samples_out',
    'phy.tx_sample_out--phy.out',
    'phy.tx_samples_out--phy.out',
    # Uplink
    'phy.rx_samples_start--phy.rx_samples_in',
    'phy.rx_samples_in--phy.fft',
    'phy.fft--phy.prach_pucch',
    'phy.prach_pucch--phy.CH_est',
    'phy.CH_est--phy.demodulated',
    'phy.CB_dec--phy.dec_fail',
    'phy.dec_fail--phy.prach_pucch',
    'phy.CB_dec--phy.TB_dec',
    'phy.TB_dec--phy.srs',
    'phy.srs--phy.rach_uci',
    'mac.demuxed--rlc.dec',
    'rlc.dec--rlc.reassembled',
    'rlc.reassembled--pdcp.hdr_dec',
    'pdcp.hdr_dec--pdcp.int_ciph_dec',
    'pdcp.int_ciph_dec--pdcp.deliver',
    'pdcp.deliver--sdap.sdu',
    'pdcp.deliver--pdcp.deliver_ooo',
    'pdcp.deliver_ooo--sdap.sdu',
    'pdcp.deliver--pdcp.deliver_reorder',
    'pdcp.deliver_reorder--sdap.sdu'
}

KWS_IN_D = {'sdap.pdu'}
KWS_OUT_D = {'phy.out'}
KWS_IN_U = {'phy.SOUTHstart', 'phy.rx_samples_start'}
KWS_OUT_U = {'gtp.out', 'phy.retx_drop', 'pdcp.discard_rcvdsmallerdeliv'}

VERBOSITY = True
MULTIPROCESSING = False
TRIM = False


class LatSeqLogParser:
    """
    Parses latseq log files efficiently using generators to minimize RAM usage.
    """
    
    def __init__(self, filepath: str):
        logger.info(f"Initialize {self.__class__.__name__}")
        
        self.filepath = Path(filepath)

        # 1. Get total line count for tqdm without loading the file into memory
        total_lines = self._count_lines(self.filepath)
        
        # 2. Get a generator that yields lines one by one
        lines_generator = self._read_log_file(self.filepath)
        
        # 3. Parse the generator.
        self.events = self._parse_all_events(lines_generator, total_lines)
        
        logger.info(f"Parsed {len(self.events)} events in {self.__class__.__name__}")

    def _count_lines(self, log_file_path: Path) -> int:
        """Efficiently counts the total lines using byte chunks."""
        try:
            with open(log_file_path, 'rb') as f:
                return sum(buf.count(b'\n') for buf in iter(lambda: f.read(1024 * 1024), b''))
        except FileNotFoundError:
            logger.error(f"File not found at: {log_file_path}")
            raise

    def _read_log_file(self, log_file_path: Path) -> Generator[str, None, None]:
        try:
            with open(log_file_path, 'r') as f:
                for line in f:
                    yield line 
        except FileNotFoundError:
            logger.error(f"File not found at: {log_file_path}")
            raise

    def _parse_event_line(self, log_line, line_num):
        log_line = log_line.strip()
        if not log_line:
            return None

        try:
            # Native JSON loading from pre-converted format
            raw_data = json.loads(log_line)
        except (json.JSONDecodeError, TypeError):
            return None

        # --- FILTERING LOGIC START ---
        properties = raw_data.get('properties', {})
        if 'mac_sdu_sz' in properties:
            try:
                # Safely cast to float/int to handle potential string types
                sz = float(properties['mac_sdu_sz'])
                if sz <= 3:
                    return None  # Discard if it exists but is not > 3
            except (ValueError, TypeError):
                # If it exists but is un-parsable as a number, decide if you want to keep or discard.
                # Keeping it is usually safer:
                pass
        # --- FILTERING LOGIC END ---

        # Map base elements into your existing trace layout schema
        parsed_event = {
            'line_num': line_num,
            'ts': Decimal(str(raw_data['ts'])),  # Direct map preserving decimal precision
            'dir': raw_data.get('dir'),
            'src': raw_data.get('src'),
            'dest': raw_data.get('dest'),
        }

        parsed_event['properties'] = raw_data.get('properties', {})
        parsed_event['globalIDs'] = raw_data.get('globalIDs', {})
        parsed_event['localIDs'] = raw_data.get('localIDs', {})

        return parsed_event

    def _parse_all_events(self, raw_lines_gen: Generator[str, None, None], total_lines: int) -> list[dict]:
        logger.info(f"Starting to parse {total_lines} lines")
        events = []

        for line_num, line in tqdm(
            enumerate(raw_lines_gen, start=1),
            total=total_lines,
            desc="Parsing log",
            unit="line",
            disable=not VERBOSITY
        ):
            event = self._parse_event_line(line, line_num)
            if event is not None:
                events.append(event)

        logger.info(f"Log file parsed: {len(events)} events")
        return events

    def get_startpoint_events(self) -> list[dict]:
        return [e for e in self.events if e['src'] in KWS_IN_D or e['src'] in KWS_IN_U]

    def get_uplink_events_by_src(self):
        uplink_by_src = defaultdict(list)
        for event in self.events:
            if event['dir'] == 'U' and event['src'] not in KWS_IN_U:
                uplink_by_src[event['src']].append(event)

        final_lookup = {}
        for src, events in uplink_by_src.items():
            events.sort(key=lambda e: e['ts'])
            timestamps = [e['ts'] for e in events]
            final_lookup[src] = {"events": events, "timestamps": timestamps}

        return final_lookup

    def get_downlink_events_by_src(self) -> dict[str, list[dict]]:
        downlink_by_src = defaultdict(list)
        for event in self.events:
            if event['dir'] == 'D' and event['src'] not in KWS_IN_D:
                downlink_by_src[event['src']].append(event)
        
        final_lookup = {}
        for src, events in downlink_by_src.items():
            events.sort(key=lambda e: e['ts'])
            timestamps = [e['ts'] for e in events]
            final_lookup[src] = {"events": events, "timestamps": timestamps}
        
        return final_lookup


class LatSeqJourneyRebuilder:
    def __init__(
        self,
        latseq_log_parser: LatSeqLogParser,
        output_file_path: str | None = None,
        write_to_stdout: bool = True,
    ):
        logger.info(f"Initialize {self.__class__.__name__}")

        self.startpoints: list[dict] = latseq_log_parser.get_startpoint_events()
        self.uplink_events_by_src: dict[str, dict] = latseq_log_parser.get_uplink_events_by_src()
        self.downlink_events_by_src: dict[str, dict] = latseq_log_parser.get_downlink_events_by_src()

        self.output_file_path = output_file_path
        self.write_to_file = bool(output_file_path)
        self.write_to_stdout = write_to_stdout

        self.journeys: list[dict] = []

        logger.info(
            f"Initialized {self.__class__.__name__} with "
            f"{len(self.startpoints)} startpoints, "
            f"{sum(len(e['events']) for e in self.uplink_events_by_src.values())} uplink events, "
            f"{sum(len(e['events']) for e in self.downlink_events_by_src.values())} downlink events"
        )

    def rebuild_journeys(self) -> None:
        logger.info(f"Starting to rebuild journeys from {len(self.startpoints)} startpoints")

        local_journeys = []

        for start_event in tqdm(
            self.startpoints,
            desc="Rebuilding journeys",
            unit="startpoint",
            disable=not VERBOSITY,
        ):
            try:
                start_time = perf_counter()
                journeys = self._rebuild_journeys_from_startpoint(start_event)
                rebuild_ms = 1000 * (perf_counter() - start_time)

                if not journeys:
                    continue

                for j in journeys:
                    j['rebuild_time_ms'] = rebuild_ms
                    if j.get('completed'):
                        local_journeys.append(j)

            except Exception as e:
                logger.error(f"Error rebuilding journeys from startpoint {start_event}: {e}")
                continue

        logger.info(f"Journeys rebuilt: {len(local_journeys)}")

        local_journeys = self._finalize_journeys(local_journeys)
        self.journeys = local_journeys
        logger.info("Journeys finalized and packet IDs assigned.")

    def _rebuild_journeys_from_startpoint(self, start_event: dict) -> list[dict]:
        journeys: list[dict] = [self._create_initial_journey(start_event)]

        event_lookup = (
            self.uplink_events_by_src if start_event['dir'] == 'U'
            else self.downlink_events_by_src
        )

        while not self._all_journeys_finished(journeys):
            for journey_idx, journey in enumerate(journeys):
                if journey['completed'] or journey['stuck']:
                    continue

                next_point = journey['events'][-1]['dest']
                candidate_next_events = event_lookup.get(next_point)

                if not candidate_next_events:
                    journey['stuck'] = True
                    continue

                matching_events = self._find_matching_events_for_journey(
                    journey, candidate_next_events
                )
                
                if not matching_events:
                    journey['stuck'] = True
                    continue

                if len(matching_events) > 1:
                    self._branch_journey_for_multiple_matches(journeys, journey_idx, matching_events)
                else:
                    self._extend_journey_with_event(journey, matching_events[0])

        return journeys

    def _create_initial_journey(self, start_event: dict) -> dict:
        return {
            'completed': False,
            'stuck': False,
            'dir': start_event['dir'],
            'events': [start_event]
        }

    def _compute_journey_metadata(self, journey: dict) -> None:
        journey['events'].sort(key=lambda e: e['ts'])

        journey.pop('stuck', None)
        journey.pop('completed', None)

        journey['ts_in'] = journey['events'][0]['ts']
        journey['ts_out'] = journey['events'][-1]['ts']

        journey['latency'] = journey['ts_out'] - journey['ts_in']
        journey['latency_ms'] = 1000 * journey['latency']

        journey['properties'] = {}
        journey['globalIDs'] = {}
        journey['localIDs'] = {}
        for event in journey['events']:
            self._update_collect(journey['properties'], event['properties'])
            self._update_collect(journey['globalIDs'], event['globalIDs'])
            self._update_collect(journey['localIDs'], event['localIDs'])

    def _update_collect(self, existing: dict, new: dict) -> None:
        for k, v in new.items():
            if k not in existing:
                existing[k] = v
                continue

            current = existing[k]

            if isinstance(current, list):
                if v not in current:
                    current.append(v)
            else:
                if v != current:
                    existing[k] = [current, v]

    def _clone_journey_dict(self, j):
        clone = j.copy()
        clone['events'] = j['events'].copy()
        return clone

    def _branch_journey_for_multiple_matches(self, journeys: list[dict], base_idx: int, matches: list[dict]) -> None:
        base_journey = journeys[base_idx]

        segmented_journeys = [base_journey]
        segmented_journeys.extend(self._clone_journey_dict(base_journey) for _ in range(len(matches) - 1))

        for journey, match in zip(segmented_journeys, matches):
            self._extend_journey_with_event(journey, match)

        journeys.extend(segmented_journeys[1:])

    def _extend_journey_with_event(self, journey: dict, event: dict) -> None:
        journey['events'].append(event)
        next_point = event['dest']
        if next_point in KWS_OUT_U or next_point in KWS_OUT_D:
            journey['completed'] = True

    def _all_journeys_finished(self, journeys: list[dict]) -> bool:
        return all(j['completed'] or j['stuck'] for j in journeys)

    def _find_matching_events_for_journey(self, journey: dict, src_data: dict) -> list[dict]:
        candidate_events = src_data["events"]
        timestamps = src_data["timestamps"]

        last_event = journey['events'][-1]
        prev_local_ids = last_event['localIDs']
        prev_ts = last_event['ts']

        left = bisect.bisect_left(timestamps, prev_ts)
        right = bisect.bisect_right(timestamps, prev_ts + DURATION_TO_SEARCH_PKT)
        candidates_in_window = candidate_events[left:right]

        # Hoisted string formatting to occur exactly once rather than N times
        no_segmentation = f"{last_event['src']}--{last_event['dest']}" in KWS_NO_SEGMENTATION

        matched_events = []
        for event in candidates_in_window:
            event_local_ids = event['localIDs']
            
            # Swapped expensive set intersections for raw dictionary checks
            shared_found = False
            match_valid = True
            for k, v in prev_local_ids.items():
                if k in event_local_ids:
                    shared_found = True
                    if event_local_ids[k] != v:
                        match_valid = False
                        break
            
            if shared_found and match_valid:
                matched_events.append(event)
                
                if no_segmentation:
                    return matched_events

        return matched_events

    def journeys_to_json(self, output_file_path=None):
        if not self.journeys:
            self.rebuild_journeys()
    
        logger.info("Serializing journeys to JSON Lines")

        # Custom fallback handler to turn Decimals into standard floats
        def decimal_serializer(obj):
            if isinstance(obj, Decimal):
                return float(obj)
            raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
    
        def json_gen():
            for journey in self.journeys:
                yield json.dumps(journey, default=decimal_serializer)
    
        path = output_file_path if output_file_path else self.output_file_path
    
        if self.write_to_file and path:
            self._write_to_file(json_gen(), path)
    
        if self.write_to_stdout:
            self._write_to_stdout(json_gen())
    
    def _write_to_file(self, json_gen, file_path):
        logger.info(f"Writing journeys to file: {file_path}")
        with open(file_path, "w", encoding="utf-8") as f:
            for json_str in json_gen:
                f.write(json_str + "\n")
        logger.info(f"Finished writing journeys to {file_path}")

    def _write_to_stdout(self, json_gen):
        logger.info("Writing journeys to stdout")
        for json_str in json_gen:
            print(json_str)
        logger.info("Finished writing journeys to stdout")

    def _assign_journey_ids(self, journeys):
        for jid, j in enumerate(journeys):
            j['journey_id'] = jid
        return journeys

    def _assign_packet_ids(self, journeys):
        packet_map = {}
        packet_id = 0

        for journey in journeys:
            direction = journey.get('dir')
            events = journey.get('events')

            if direction not in ('U', 'D') or not events:
                journey['packet_id'] = None
                continue

            first_line = events[0]['line_num']
            last_line = events[-1]['line_num']

            key = first_line if direction == 'D' else last_line

            if key not in packet_map:
                packet_map[key] = packet_id
                packet_id += 1

            journey['packet_id'] = packet_map[key]

        return journeys

    def _custom_sort_keys_of_journeys(self, journeys):
        CUSTOM_ORDER = ["dir", "packet_id", "journey_id", "latency", "latency_ms", "ts_in", "ts_out", "rebuild_time_ms", "localIDs", "globalIDs", "properties"]
        ordered_journeys = []

        for journey in journeys:
            ordered = {k: journey[k] for k in CUSTOM_ORDER if k in journey}
            for key, val in journey.items():
                if key not in ordered:
                    ordered[key] = val
            ordered_journeys.append(ordered)

        return ordered_journeys

    def _tmp_clean_journeys(self, journeys):
        for j in journeys:
            j.pop('globalIDs', None)
            for e in j.get('events', []):
                e.pop('dir', None)
                e.pop('globalIDs', None)

        return journeys

    def _finalize_journeys(self, journeys):
        for j in journeys:
            self._compute_journey_metadata(j)

        journeys = self._assign_journey_ids(journeys)
        journeys = self._assign_packet_ids(journeys)
        journeys = self._custom_sort_keys_of_journeys(journeys)
        journeys = self._tmp_clean_journeys(journeys)
        return journeys


# --- Main Execution Block ---

def main():
    parser = argparse.ArgumentParser(
        description="Reconstruct individual packet traces and calculate latency from latseq log files."
    )

    parser.add_argument(
        "-l", "--log-file",
        required=True,
        help="Path to the input latseq log file (e.g., unix_time.lseq)."
    )

    parser.add_argument(
        "-o", "--output-file-path",
        help="Optional path to write journeys as JSON (e.g., ./journeys/journeys_separated.lseqj). "
             "If provided, journeys will be written to this file."
    )

    parser.add_argument(
        "--no-stdout",
        action="store_true",
        help="Disable printing journeys to stdout."
    )

    parser.add_argument(
        "-j", "--journeys",
        action="store_true",
        help="Convert parsed journeys to JSON (calls journeys_to_json())."
    )

    args = parser.parse_args()

    input_log_path = args.log_file
    log_processor = LatSeqLogParser(input_log_path)

    journey_rebuilder = LatSeqJourneyRebuilder(
        log_processor,
        output_file_path=args.output_file_path,
        write_to_stdout=not args.no_stdout,
    )

    if args.journeys:
        journey_rebuilder.journeys_to_json()
    

if __name__ == "__main__":
    main()