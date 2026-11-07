# Matchup Bot

Scans every upcoming **NFL, college football, NBA and men's college basketball** game and sends a phone alert when one team's unit is top-tier and the unit it faces is bottom-tier. For example: an elite run game against a bottom-5 run defense, or a top pass rush against a leaky offensive line. It doesn't pick bets. It only points out the lopsided matchups.

## One-time setup (Windows, ~10 min)

1. **Install Python 3.11 or newer** from <https://www.python.org/downloads/>. On the first installer screen, tick **"Add python.exe to PATH"**.
2. **Unzip this folder** somewhere permanent, e.g. `Documents\matchup-bot`.
3. **Set up phone alerts:** install the free **ntfy** app (App Store / Google Play), tap **+**, enter the topic `jake-matchups-38ff91b6` and tap Subscribe. The topic name is already in `config.toml`.
4. **Double-click `run_bot.bat`.** The first run downloads the season so far, which takes a few minutes. After that it sends any alerts and opens the report in your browser. Later runs take seconds.
5. **Double-click `schedule_daily.bat`** to have it run automatically every day at 10:00 AM. If the PC is off at 10, it runs when you're back on. A console window pops up for a few seconds while it runs; that's normal.

Test the phone alerts by themselves: open the folder, type `cmd` in the address bar, press Enter, then run `py matchup_bot.py --test-notify`.

## What you get

**Push notification** (one per league, only for new mismatches):

```
🏈 NFL: 5 new mismatches in 3 games
Ranks out of ~32 teams, schedule-adjusted.

BUF @ MIA · Sun 11/08 12:00 PM
• Yards per carry: BUF run game #2 (5.31) vs MIA run defense #31 (5.12)
• Sack rate: BUF pass rush #3 (9.4%) vs MIA pass protection #30 (9.9%)
```

The team listed first in each line has the edge. Numbers in parentheses are raw season averages; for a defense, that's what it allows.

**`reports/latest.html`**: the full report, including games not in the notification. Each run also saves a dated copy.

**`reports/<league>_team_ranks.csv`**: every team's offense and defense rank in every stat. Open it in Excel for your own research.

## How it decides what's "unfair"

1. **Ranks every unit in every stat.** Rank 1 is the best offense or the stingiest defense.
2. **Adjusts for schedule.** A defense that only faced bad offenses is pulled back toward average. This matters most in college.
3. **Ignores stats that are still noise.** For each stat, the bot measures how much of the spread between teams is real and how much is small-sample luck. Stats under 30% real (turnovers and red-zone % often fail this) are skipped until they stabilize. The report lists which stats are currently skipped.
4. **Flags a game** when one side is at or above the "strong" percentile and the unit it faces is at or below the "weak" percentile.
5. **Skips obvious blowouts in college.** If the two teams' overall ratings are miles apart, the game is skipped, since everyone already knows it's lopsided. Games marked ⚡ *evenly matched overall* are the most interesting: the teams are close overall but one unit has a big edge.
6. **Never repeats an alert** for the same game and the same unit matchup.

### Stats compared (offense vs. the defense it faces)

| Football (NFL + NCAAF) | Basketball (NBA + NCAAB) |
|---|---|
| Points per game, points per drive* | Points per 100 possessions |
| Yards per play | Effective FG% |
| Rushing yards/game, yards per carry | 3-point %, 3-point attempt rate |
| Net passing yards/game, net yards per attempt | 2-point % |
| Completion % | Turnover rate (ball security vs. pressure) |
| Sack rate (protection vs. pass rush)* | Offensive rebound % vs. defensive rebounding |
| 3rd-down % | Free-throw rate |
| Red-zone TD %* | Paint points*, fast-break points* (per 100) |
| Turnovers (ball security vs. takeaways) | Shots blocked (finishing vs. rim protection) |
| First downs/game | Assist rate |

\*Only where ESPN's box scores include it (mostly the pro leagues).

## Tuning (`config.toml`, open with Notepad)

- **Too many alerts?** Raise `strong_pct` and lower `weak_pct` for that league (e.g. 90/10 → 95/5).
- **Too few?** Do the opposite, or lower `min_reliability` to 0.2.
- **Don't care about a stat?** Add its key to `disabled` under `[metrics]`.
- **Also want alerts in Discord?** Paste a channel webhook URL into `discord_webhook`.
- `lookahead_days`: football scans 7 days ahead, basketball scans today only.

## Commands

Run these from a terminal in the folder (or add them after `run_bot.bat`):

```
py matchup_bot.py                     run everything (same as double-clicking run_bot.bat)
py matchup_bot.py --leagues nfl,nba   only some leagues
py matchup_bot.py --dry-run           show alerts in the window, don't send to phone
py matchup_bot.py --resend            re-send alerts that were already sent
py matchup_bot.py --test-notify       send a test push
```

## Good to know

- **Data source:** ESPN's free public JSON feeds (no account or key). They're unofficial, so if ESPN changes them, `logs/last_run.log` will say "parsed 0 box scores". That's the sign something needs updating.
- **Box-score stats only.** There's no play-by-play data (EPA, success rate, explosive plays) and no injury or lineup info. Check who's actually playing before you act on an alert.
- **College basketball** just started, so teams have only 1–2 games. Alerts start once teams have 3 games, and more stats switch on as samples grow (late Nov–Dec).
- To reset everything, delete the `cache` and `state` folders.
