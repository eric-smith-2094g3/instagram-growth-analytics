# instagram-growth-analytics

I got tired of paying for social analytics dashboards I barely used, so I wrote this to snapshot follower counts and basic public metrics for accounts I care about. Data stays local, runs on a cron job.

## install

pip install -r requirements.txt

## usage

Handles are stored in ~/.config/ig-growth/handles.json. Snapshots append to a local sqlite file. No network calls happen if the handles list is empty.
