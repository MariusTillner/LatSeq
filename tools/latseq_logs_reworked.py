#!/usr/bin/python3

import logging
import argparse
import sys
import re
from decimal import Decimal
from pathlib import Path
from tqdm import tqdm
from collections import defaultdict
import bisect
from time import perf_counter
import simplejson as json


logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(funcName)s] %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

# Reducing search space
DURATION_TO_SEARCH_PKT = Decimal(0.8) # USED to avoid accidental mismatch of points which are too far apart in the time domain, 0.05 are 50ms

# CONSTANTS
S_TO_MS = 1000

# full name of all points where segmentation can't happen; the user has to know if segmentation can happen; this improves perfomance as unnecessary search is avoided
#KWS_NO_SEGMENTATION = [
#                        'mac.demuxed--rlc.dec',
#                        'rlc.reassembled--pdcp.dec', // why?!?!
#                        'pdcp.dec--sdap.sdu',
#                        'pdcp.outoforderdeliv--sdap.sdu',
#                        'pdcp.reorderdeliv--sdap.sdu',
#                        'sdap.sdu--pdcp.hdr',
#                        'rlc.seg--mac.handover',
#                        'phy.retxdecfail--phy.prachpucch',
#                        'phy.CBdec--phy.retxdecfail',
#                        'mac.handover--mac.hdr', // why?!?!
#                        'mac.hdr--mac.retx'
#                      ]

KWS_NO_SEGMENTATION = [
                        # Downlink
                        'sdap.sdu--pdcp.hdr',
                        'pdcp.hdr--rlc.buffer',
                        'rlc.buffer--rlc.seg',
                        'rlc.seg--mac.handover',
                        'mac.hdr--mac.retx',
                        'mac.hdr--mac.dci',
                        'phy.cbseg--phy.ldpc',
                        'phy.ldpc--phy.scrambled',
                        'phy.scrambled--phy.modulated',
                        'phy.modulated--phy.resourcemapped',
                        'phy.resourcemapped--phy.antennamapped',
                        'phy.antennamapped--phy.csi',
                        'phy.csi--phy.ofdmidft',
                        'phy.ofdmidft--phy.SOUTHout',
                        # Uplink
                        'phy.SOUTHstart--phy.SOUTHend',
                        'phy.SOUTHend--phy.dft',
                        'phy.dft--phy.prachpucch',
                        'phy.CHest--phy.rbscalvl',
                        'phy.rbscalvl--phy.symbolproc',
                        'phy.TBdec--phy.srs',
                        'phy.srs--phy.rachuci',
                        'phy.retxdecfail--phy.prachpucch',
                        'phy.CBdec--phy.retxdecfail',
                        'mac.demuxed--rlc.dec',
                        'rlc.dec--rlc.reassembled',
                        'pdcp.reorderdeliv--sdap.sdu',
                        'pdcp.outoforderdeliv--sdap.sdu',
                        'pdcp.dec--sdap.sdu'
]

KWS_IN_D = ['sdap.sdu']
KWS_OUT_D = ['phy.out']
KWS_IN_U = ['phy.SOUTHstart']
KWS_OUT_U = ['gtp.out', 'phy.retxdrop', 'pdcp.rcvdsmallerdeliv']
VERBOSITY = True  # Verbosity for rebuild phase False by default; only shows progress bar when MULTIPROCESSING is False
MULTIPROCESSING = False # can be set to true in file or with args (see args.multiprocessing)
TRIM = False # can be set to true in file or with args (see args.trimlog), trims the input log file to 2 sec before and after the first/last meaningful line in the log


class LatSeqLogParser:
    """Parses and reconstructs latency journeys from latseq log files."""
    
    def __init__(self, filepath: str):
        logger.info(f"Initialize {self.__class__.__name__}")
        self.filepath = Path(filepath)
        self.raw_lines = self._read_log_file(filepath)
        logger.info(f"Loaded {len(self.raw_lines)} lines from {self.filepath}")
        self.events = self._parse_all_events()
        logger.info(f"Parsed {len(self.events)} events in {self.__class__.__name__}")

    # Renamed to reflect the action of loading raw data
    def _read_log_file(self, log_file_path) -> list:
        """Reads the log file and returns a list of raw lines."""
        raw_trace_lines = []
        try:
            # Use 'f' for the standard file handle
            with open(log_file_path, 'r') as f:
                raw_trace_lines = f.readlines()
        except FileNotFoundError:
            # ... error handling
            pass
            
        return raw_trace_lines

    def _parse_kv_string(self, kv_string):
        if not kv_string:
            return {}
        
        parsed_dict = {}
        items = kv_string.split('.')
        
        for item in items:
            key_end_index = 0
            
            # Find the index of the first digit (start of value)
            for i, char in enumerate(item):
                if char.isdigit() or (char == '-' and i + 1 < len(item) and item[i + 1].isdigit()):
                    key_end_index = i
                    break
                
            if key_end_index > 0:
                key = item[:key_end_index]
                value_str = item[key_end_index:]
    
                try:
                    value = int(value_str)
                except ValueError:
                    value = value_str  # fallback, in case of unexpected formats
    
                parsed_dict[key] = value
    
        return parsed_dict


    def _parse_event_line(self, log_line, line_num):
        """
        Parses a single latseq log line assuming the Uplink (UL) format:
        [TS] U [src--dest] [prop]:[globalIDs]:[localIDs]...

        Example: 1758902154.6302049 U mac.handover--mac.hdr size1000:rnti9199:RMbuf3547781203.fm523.sl12.hqpid5
        """
        timestamp_str, direction, src_dest_field, param_string = log_line.split(" ")

        src, dest = src_dest_field.split('--')

        # 2. Split the parameter string by the colon (':') to separate the categories
        # This relies on the convention that the categories are strictly ordered.
        param_parts = [p.strip() for p in param_string.split(':')]

        # 3. Positional Assignment based on the required output structure
        # prop: The first segment
        prop_segment = param_parts[0] if len(param_parts) >= 1 else ""

        # globalIDs: The second segment
        global_ids_str = param_parts[1] if len(param_parts) >= 2 else ""

        # localIDs: The third segment and all subsequent segments
        local_ids_str = param_parts[2] if len(param_parts) >= 3 else ""

        # 4. Final Dictionary Construction
        parsed_event = {}
        parsed_event['line_num'] = line_num
        parsed_event['ts'] = Decimal(timestamp_str)
        parsed_event['dir'] = direction
        parsed_event['src'] = src
        parsed_event['dest'] = dest
        parsed_event['prop'] = self._parse_kv_string(prop_segment)
        parsed_event['globalIDs'] = self._parse_kv_string(global_ids_str)
        parsed_event['localIDs'] = self._parse_kv_string(local_ids_str)

        return parsed_event

    def _parse_all_events(self) -> list[dict]:
        """Parse all raw log lines into structured event dictionaries with a progress bar."""
        total_lines = len(self.raw_lines)
        logger.info(f"Starting to parse {total_lines} lines")
        events = []

        for line_num, line in tqdm(
            enumerate(self.raw_lines, start=1),
            total=total_lines,
            desc="Parsing log",
            unit="line",
            disable=not VERBOSITY  # Optional: hide when verbosity is off
        ):
            event = self._parse_event_line(line, line_num)
            if event is not None:
                events.append(event)

        logger.info(f"Log file parsed: {len(events)} events")
        return events

    def get_startpoint_events(self) -> list[dict]:
        """Return a list of events that are considered startpoints for journeys."""
        startpoints = []
        for event in self.events:
            if event['src'] in KWS_IN_D or event['src'] in KWS_IN_U:
                startpoints.append(event)
        return startpoints

    def get_uplink_events_by_src(self):
        uplink_by_src = {}
        for event in self.events:
            if event['dir'] == 'U' and event['src'] not in KWS_IN_U:
                uplink_by_src.setdefault(event['src'], []).append(event)

        # Sort events per src and build timestamps array once
        for src, events in uplink_by_src.items():
            events.sort(key=lambda e: e['ts'])
            timestamps = [e['ts'] for e in events]
            uplink_by_src[src] = {"events": events, "timestamps": timestamps}

        return uplink_by_src


    def get_downlink_events_by_src(self) -> dict[str, list[dict]]:
        downlink_by_src = {}
        for event in self.events:
            if event['dir'] == 'D' and event['src'] not in KWS_IN_D:
                downlink_by_src.setdefault(event['src'], []).append(event)
    
        # Sort events per src and build timestamps array once
        for src, events in downlink_by_src.items():
            events.sort(key=lambda e: e['ts'])
            timestamps = [e['ts'] for e in events]
            downlink_by_src[src] = {"events": events, "timestamps": timestamps}
    
        return downlink_by_src


class LatSeqJourneyRebuilder:
    def __init__(
        self,
        latseq_log_parser: LatSeqLogParser,
        output_file_path: str | None = None,
        write_to_stdout: bool = True,
    ):
        """
        Initialize journey rebuilder with parsed events.
        """
        logger.info(f"Initialize {self.__class__.__name__}")

        # Parsed event sources
        self.startpoints: list[dict] = latseq_log_parser.get_startpoint_events()
        self.uplink_events_by_src: dict[str, dict] = latseq_log_parser.get_uplink_events_by_src()
        self.downlink_events_by_src: dict[str, dict] = latseq_log_parser.get_downlink_events_by_src()

        # Output control
        self.output_file_path = output_file_path
        self.write_to_file = bool(output_file_path)
        self.write_to_stdout = write_to_stdout

        # Internal state
        self.journeys: list[dict] = []

        logger.info(
            f"Initialized {self.__class__.__name__} with "
            f"{len(self.startpoints)} startpoints, "
            f"{sum(len(e['events']) for e in self.uplink_events_by_src.values())} uplink events, "
            f"{sum(len(e['events']) for e in self.downlink_events_by_src.values())} downlink events"
        )


    def rebuild_journeys(self) -> None:
        """Rebuild all journeys starting from startpoints."""
        logger.info(f"Starting to rebuild journeys from {len(self.startpoints)} startpoints")

        local_journeys = []

        for startpoint in tqdm(
            self.startpoints,
            desc="Rebuilding journeys",
            unit="startpoint",
            disable=not VERBOSITY,
        ):
            try:
                start_time = perf_counter()
                journeys = self._rebuild_journeys_from_startpoint(startpoint)
                rebuild_ms = 1000 * (perf_counter() - start_time)

                if not journeys:
                    continue

                # Add timing information and filter completed ones
                completed = []
                for j in journeys:
                    j['rebuild_time_ms'] = rebuild_ms
                    if j.get('completed'):
                        completed.append(j)

                local_journeys.extend(completed)

            except Exception as e:
                logger.error(f"Error rebuilding journeys from startpoint {startpoint}: {e}")
                continue

        logger.info(f"Journeys rebuilt: {len(local_journeys)}")

        # Finalize all journeys
        local_journeys = self._finalize_journeys(local_journeys)

        self.journeys = local_journeys
        logger.info("Journeys finalized and packet IDs assigned.")


    def _rebuild_journeys_from_startpoint(self, start_event: dict) -> list[dict]:
        """
        Rebuilds all possible latency journeys starting from a given startpoint.
        Handles branching caused by segmentation by maintaining multiple
        in-progress journeys in parallel.
        """
        # Start with a single initial journey
        journeys: list[dict] = [self._create_initial_journey(start_event)]

        # Select the correct event lookup dict based on direction
        event_lookup = (
            self.uplink_events_by_src if start_event['dir'] == 'U'
            else self.downlink_events_by_src
        )

        # Keep expanding journeys until all are marked finished or stuck
        while not self._all_journeys_finished(journeys):
            for journey_idx, journey in enumerate(journeys):
                if journey['completed'] or journey['stuck']:
                    continue

                next_point = journey['events'][-1]['dest']
                candidate_next_events = event_lookup.get(next_point)

                # No further matching events available
                if not candidate_next_events:
                    journey['stuck'] = True
                    continue

                # Filter events that actually match this journey (e.g. time, IDs)
                matching_events = self._find_matching_events_for_journey(
                    journey, candidate_next_events
                )
                if not matching_events:
                    journey['stuck'] = True
                    continue

                # If multiple matching events → segmentation → branch journeys
                if len(matching_events) > 1:
                    self._branch_journey_for_multiple_matches(journeys, journey_idx, matching_events)
                else:
                    # Single match → just extend journey
                    self._extend_journey_with_event(journey, matching_events[0])

        return journeys


    # --- Helper functions ---

    def _create_initial_journey(self, start_event: dict) -> dict:
        """
        Create and initialize a new journey dictionary starting from the given event.

        A *journey* represents the progression of related events between
        defined start and end points (e.g., KWS_IN_D → KWS_OUT_D).

        Structure of the returned journey dictionary:

        - **completed** (`bool`): 
            Whether the journey has reached a valid end point (KWS_OUT_D or KWS_OUT_U).
        - **stuck** (`bool`): 
            True if the journey has no possible continuation (no next point found).
        - **dir** (`str`): 
            Direction of transmission — `'U'` for uplink or `'D'` for downlink.
        - **ts_in** (`Decimal`): 
            Timestamp of the first event in the journey (set in `_compute_journey_metadata()`).
        - **ts_out** (`Decimal`): 
            Timestamp of the last event in the journey (set in `_compute_journey_metadata()`).
        - **latency** (`Decimal`): 
            Duration between `ts_out` and `ts_in` (set in `_compute_journey_metadata()`).
        - **latency_ms** (`Decimal`): 
            Duration between `ts_out` and `ts_in` in milliseconds (set in `_compute_journey_metadata()`).
        - **events** (`list[dict]`): 
            Ordered list of all events forming this journey.
        - **prop** (`dict`): 
            Aggregated event properties encountered along the journey.
        - **globalIDs** (`dict`): 
            Aggregated global IDs from all events.
        - **localIDs** (`dict`): 
            Aggregated local IDs from all events.
        - **rebuild_time_ms** (`Decimal`):  
            Time spent rebuilding this journey, measured in milliseconds (set in `rebuild_journeys()`).
        - **journey_id** (`int`):  
            Unique journey identifier assigned as a monotonically increasing counter (set in `_assign_journey_ids()`).
        - **packet_id** (`int`):  
            Identifier for the packet this journey belongs to.  
            Journeys sharing the same most nothern event (in terms of network layers), determined by their direction and the
            relevant `line_num` are assigned the same `packet_id` (set in `_assign_packet_ids()`).

        Note:
            Only minimal keys are initialized here; the rest are populated 
            during `_compute_journey_metadata()`, _assign_journey_ids() and _assign_packet_ids() once the journey is complete.
        """
        return {
            'completed': False,
            'stuck': False,
            'dir': start_event['dir'],
            'events': [start_event]
            # Remaining fields are filled in `_compute_journey_metadata(), _assign_journey_ids() and _assign_packet_ids()`
        }


    def _compute_journey_metadata(self, journey: dict) -> None:
        """
        Finalize a journey by computing latency and merging collected event data.
        Removes temporary fields, calculates latency, and aggregates
        per-event properties and IDs into the journey-level dictionaries.
        """
        # sort events of journey from oldest timestamp to latest timestamp
        journey['events'].sort(key=lambda e: e['ts'])

        # Clean up unused fields
        journey.pop('stuck', None)
        journey.pop('completed', None)

        # Set timestamps
        journey['ts_in'] = journey['events'][0]['ts']
        journey['ts_out'] = journey['events'][-1]['ts']

        # Compute latency
        journey['latency'] = journey['ts_out'] - journey['ts_in']
        journey['latency_ms'] = 1000 * journey['latency']

        # Merge all event-level data into journey-level dicts
        journey['prop'] = {}
        journey['globalIDs'] = {}
        journey['localIDs'] = {}
        for event in journey['events']:
            self._update_collect(journey['prop'], event['prop'])
            self._update_collect(journey['globalIDs'], event['globalIDs'])
            self._update_collect(journey['localIDs'], event['localIDs'])


    def _update_collect(self, existing: dict, new: dict) -> None:
        """
        Merge key-value pairs from `new` into `existing`.

        - If a key is not in `existing`, it's added.
        - If a key exists:
            - If both values are scalars → convert to list with both.
            - If either is a list → flatten and extend.
        Modifies `existing` in place.
        """
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
        """
        Handle segmentation: extend the original journey with the first match,
        and clone it for subsequent matches.
        """
        base_journey = journeys[base_idx]

        # Build list: original + clones for remaining matches
        segmented_journeys = [base_journey]
        segmented_journeys.extend(self._clone_journey_dict(base_journey) for _ in range(len(matches) - 1))

        # Extend each segmented journey with its corresponding match
        for journey, match in zip(segmented_journeys, matches):
            self._extend_journey_with_event(journey, match)

        # Add only the cloned journeys back into the main list
        journeys.extend(segmented_journeys[1:])


    def _extend_journey_with_event(self, journey: dict, event: dict) -> None:
        """
        Append a matched event to a journey and update its next expected point.
        """
        journey['events'].append(event)
        next_point = event['dest']
        if next_point in KWS_OUT_U or next_point in KWS_OUT_D:
            journey['completed'] = True


    def _all_journeys_finished(self, journeys: list[dict]) -> bool:
        """
        Return True if all journeys are either completed or stuck.
        """
        return all(j['completed'] or j['stuck'] for j in journeys)


    def _find_matching_events_for_journey(self, journey: dict, src_data: dict) -> list[dict]:
        """
        Match candidate events to the current journey based on timing, IDs, etc.
        """
        candidate_events = src_data["events"]
        timestamps = src_data["timestamps"]

        last_event = journey['events'][-1]
        prev_local_ids = last_event['localIDs']
        prev_ts = last_event['ts']

        # Candidate filtering using precomputed timestamps
        left = bisect.bisect_left(timestamps, prev_ts)
        right = bisect.bisect_right(timestamps, prev_ts + DURATION_TO_SEARCH_PKT)
        candidates_in_window = candidate_events[left:right]

        matched_events = []
        for event in candidates_in_window:
            event_local_ids = event['localIDs']
            common_keys = prev_local_ids.keys() & event_local_ids.keys()
            if common_keys and all(prev_local_ids[k] == event_local_ids[k] for k in common_keys):
                matched_events.append(event)
                
                # if the current point cannot have segmentation by user definition (used to avoid accidental mismatch) then don't
                # look for futher matches if first match is found
                if f"{last_event['src']}--{last_event['dest']}" in KWS_NO_SEGMENTATION:
                    return matched_events

        return matched_events


    def journeys_to_json(self, output_file_path=None):
        """
        Convert journeys to JSON and optionally write to file and/or stdout.
        """
        if not self.journeys:
            self.rebuild_journeys()
    
        logger.info("Serializing journeys to JSON")
    
        def json_gen():
            yield json.dumps(self.journeys, default=str)
    
        # Determine the output path explicitly
        path = output_file_path if output_file_path else self.output_file_path
    
        # Write to file if enabled and path available
        if self.write_to_file and path:
            self._write_to_file(json_gen(), path)
    
        # Write to stdout if enabled
        if self.write_to_stdout:
            self._write_to_stdout(json_gen())
    

    def _write_to_file(self, json_gen, file_path):
        """
        Write JSON lines to a file.
        """
        logger.info(f"Writing journeys to file: {file_path}")
        with open(file_path, "w", encoding="utf-8") as f:
            for json_str in json_gen:
                f.write(json_str + "\n")
        logger.info(f"Finished writing journeys to {file_path}")


    def _write_to_stdout(self, json_gen):
        """
        Write JSON lines to stdout.
        """
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

            # Validate structure
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
        CUSTOM_ORDER = ["dir", "packet_id", "journey_id", "latency", "latency_ms", "ts_in", "ts_out", "rebuild_time_ms", "localIDs", "globalIDs", "prop"]
        ordered_journeys = []

        for journey in journeys:
            ordered = {}
            for key in CUSTOM_ORDER:
                if key in journey:
                    ordered[key] = journey[key]

            for key in journey:
                if key not in ordered:
                    ordered[key] = journey[key]

            ordered_journeys.append(ordered)

        return ordered_journeys


    def _finalize_journeys(self, journeys):
        for j in journeys:
            self._compute_journey_metadata(j)

        journeys = self._assign_journey_ids(journeys)
        journeys = self._assign_packet_ids(journeys)
        journeys = self._custom_sort_keys_of_journeys(journeys)
        return journeys


# --- Main Execution Block ---

def main():
    """
    Parses command-line arguments and executes the journey reconstruction task.
    """
    parser = argparse.ArgumentParser(
        description="Reconstruct individual packet traces and calculate latency from latseq log files."
    )

    # Input log file (required)
    parser.add_argument(
        "-l", "--log-file",
        required=True,
        help="Path to the input latseq log file (e.g., unix_time.lseq)."
    )

    # Optional output file (implies writing to file)
    parser.add_argument(
        "-o", "--output-file-path",
        help="Optional path to write journeys as JSON (e.g., ./journeys/journeys_separated.lseqj). "
             "If provided, journeys will be written to this file."
    )

    # Disable stdout output
    parser.add_argument(
        "--no-stdout",
        action="store_true",
        help="Disable printing journeys to stdout."
    )

    # Run journey-to-JSON step
    parser.add_argument(
        "-j", "--journeys",
        action="store_true",
        help="Convert parsed journeys to JSON (calls journeys_to_json())."
    )

    args = parser.parse_args()

    # Prepare parser and rebuilder
    input_log_path = args.log_file
    log_processor = LatSeqLogParser(input_log_path)

    journey_rebuilder = LatSeqJourneyRebuilder(
        log_processor,
        output_file_path=args.output_file_path,
        write_to_stdout=not args.no_stdout,
    )

    # Run only if -j/--journeys is passed
    if args.journeys:
        journey_rebuilder.journeys_to_json()
    

if __name__ == "__main__":
    main()