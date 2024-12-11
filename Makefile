all: unix_timestamp build_seperated_journeys build_rest_journeys build_points

lseq_filename := $(wildcard *.lseq)
tools := ../../tools/

setup:
	@sed -i '/^$$/d' $(lseq_filename)
	@echo '' >> $(lseq_filename)
	@mkdir -p journeys
	@mkdir -p points
	@mkdir -p figures
	
unix_timestamp: setup
	../../tools/rdtsctots.py $(lseq_filename) > unix_time.lseq
	
build_seperated_journeys:
	$(tools)latseq_logs.py --trim-log --multiprocessing -j -l unix_time.lseq > ./journeys/journeys_separated.lseqj
	
build_rest_journeys:
	$(tools)latseq_logs.py -o -l unix_time.lseq > ./journeys/journeys_over_time.lseqj
	$(tools)latseq_stats.py -sj < ./journeys/journeys_separated.lseqj > ./journeys/journeys_stats.json
	$(tools)latseq_stats.py -djd < ./journeys/journeys_separated.lseqj > ./journeys/journeys_duration.json
	
build_points:
	$(tools)latseq_logs.py -p -l unix_time.lseq > ./points/points.json
	$(tools)latseq_stats.py -sjpp < ./journeys/journeys_separated.lseqj > ./points/delay_perPoint_perJourney.json
	$(tools)latseq_stats.py -sp < ./points/points.json > ./points/points_statistics.json
	
plotting:
	python3 $(tools)plotting_tool.py

clean:
	rm -rf ./journeys
	rm -rf ./points
	rm -rf ./figures
	rm -f unix_time.*
