# DST / time-resolution — facts verified by execution

Produced during the scrutiny pass on the time model (2026-08-20, three rounds, exited on a clean
APPROVED). Everything here was **run**, not read. The probe scripts lived in a session scratchpad
and are gone; these are the results worth keeping, because each one is the reason a specific
requirement is worded the way it is. Python 3.12, `Europe/Amsterdam` unless stated.

Transitions used throughout: forward **2026-03-29 02:00 → 03:00**, back **2026-10-25 03:00 → 02:00**.

## The detection algorithm is sound — this is the part not to touch

Ground-truth oracle (enumerate every UTC minute of the year, map to local wall clock, count
occurrences: 0 = imaginary, 1 = normal, 2 = ambiguous) versus the two-step detector
(`utcoffset() != replace(fold=1).utcoffset()`, then a UTC round-trip):

```
Europe/Amsterdam     normal=519720 ambiguous=60  imaginary=60   mismatches=0
Australia/Lord_Howe  normal=519780 ambiguous=30  imaginary=30   mismatches=0
Antarctica/Troll     normal=519600 ambiguous=120 imaginary=120  mismatches=0
+ America/Santiago, Pacific/Chatham, Asia/Kolkata, Asia/Tehran, Africa/Cairo,
  America/Havana, America/Sao_Paulo, Asia/Kathmandu, Australia/Sydney  — all mismatches=0
```

~6.2M classifications, 12 zones, zero false positives and zero false negatives. Task 4.9 commits
this oracle as a test.

## Why both steps are needed

```
AMBIGUOUS 2026-10-25 02:30  fold=0 CEST 1792888200 roundtrip 02:30 wall_same=True
                            fold=1 CET  1792891800 roundtrip 02:30 wall_same=True
IMAGINARY 2026-03-29 02:30  fold=0 CET  1774747800 roundtrip 03:30 wall_same=False
                            fold=1 CEST 1774744200 roundtrip 01:30 wall_same=False
normal reading              two offsets = False
```

Both folds of an ambiguous reading round-trip cleanly, so the round-trip cannot find them; the
offset comparison finds both kinds and cannot tell them apart. Neither step alone works.

**`fold=0` is the earlier instant for an ambiguous reading only.** For an imaginary one it is the
later (1774747800 > 1774744200). Do not generalise it.

## Traps that produced requirement text

**Aware date-times in one zone compare by wall clock, not by instant.**

```
now  fold=1 02:15 -> 1792890900     cand fold=0 02:30 -> 1792888200
cand > now  -> True   (claims future)        cand.timestamp() > now.timestamp() -> False
fold=0 == fold=1 for one reading -> True, denoting instants 3600s apart
```

**The process timezone leaks through four shapes.**

```
TZ=Europe/Amsterdam  fromtimestamp(t) -> 02:30   now() -> 10:48   naive.astimezone() -> +02:00
TZ=UTC                                -> 00:30           08:48                        +00:00
TZ=Pacific/Kiritimati                 -> 14:30           22:48                        +14:00
fromisoformat("2026-10-25T02:30").timestamp():  1792888200 / 1792895400 / 1792845000
```

The last one is the natural two-line bug: `fromisoformat` returns **naive**, and `.timestamp()` on
a naive value uses the process zone. `time.mktime` has the same property. Kiritimati changes the
*date*, which is why it is the hostile value in task 4.0.

**Durations must be added to the instant.** `aware + timedelta(days=3)` is wall-clock arithmetic:

```
spring: 71h elapsed   fall: 73h elapsed   instant arithmetic: 72h both ways
timedelta(days=1) == timedelta(hours=24) -> True   (so that assertion is a tautology)
aware + timedelta(days=1) == rebuild-on-next-calendar-date -> True
   ...and it yields an imaginary fold=0 datetime when the next date is the transition date,
   so detection must re-run on the advanced candidate
```

**Selection must precede DST evaluation on the bare `HH:MM` path.**

```
now 2026-03-29 20:00 CEST  today=imaginary future=False advanced=normal
    evaluate-first -> REJECT (wrong: the owner can have 03-30 02:30)
    select-first   -> SCHEDULE 2026-03-30 02:30
now 2026-03-29 00:30 CET   today=imaginary future=True  -> REJECT (right: they asked for tonight)
now 2026-03-28 20:00 CET   advanced=imaginary           -> REJECT (D4: no skipping a day)
now 2026-10-24 20:00 CEST  advanced=AMBIGUOUS           -> SCHEDULE fold=0 with disclosure
```

The fourth row is why the re-run applies the full nonexistent **and** ambiguous treatment.

**The repeated hour skips a valid occurrence under a fold=0-only rule.**

```
now 02:45 CEST (first pass), ask 02:30:
  +0d fold=0  1792888200  future=False      (15 min ago)
  +0d fold=1  1792891800  future=True       (45 min away — the answer)
  +1d fold=0  1792978200  future=True       (+24.75h — what fold=0-only picks)
```

**Neighbour advice cannot assume one hour.** `Antarctica/Troll` +1h is itself imaginary (2h gap);
`Australia/Lord_Howe`'s gap is 30 minutes (02:00–02:29 imaginary, 02:30 valid); `Pacific/Apia`
skipped all of 2011-12-30 (12-29 and 12-31 normal), which is why the search advances at most one
date and fully-skipped dates are out of scope.

**`fromisoformat` accepts more than expected, and midnight is indistinguishable after parsing.**

```
OK   '2026-08-25' -> 00:00     '20260825' -> 00:00     '2026-W35' / '2026-W35-1' -> 2026-08-24
OK   '…T07:30+02'  '…T07:30-00:00'  '…T07:30Z'  '…T07:30:00.000'
FAIL '…T07:30:00z' (lowercase)   ' …T07:30 ' (whitespace)   '…T24:00'
```

`2026-08-25` and `2026-08-25T00:00` parse identically, so "reject a date with no time of day" is
only decidable on the *string*. Hence whitelist-then-parse.

**Unbounded duration magnitudes raise before the horizon check.**

```
+999999999d      -> ValueError: year 2739933 is out of range        (at the arithmetic)
+99999999999999d -> OSError: Value too large for defined data type  (at the parse)
```

So the bound belongs in the grammar. `\+\d{1,6}[mhd]` is generous and sufficient.

**Offsets are the model's silent two-hour error.** `2026-08-25T07:30:00Z` → `09:30 CEST`. A
cross-check against the owner zone cannot separate "correctly means Tokyo 9am" from "wrongly
Z-suffixed a local reading" — both disagree with the owner zone — so rejection is the only check
that works.

## Zone database and rendering

```
PYTHONTZPATH="" + tzdata 2026.3  -> TZPATH=() , 598 zones, Europe/Amsterdam resolves,
                                    US/Eastern resolves (absent from this host's system tree),
                                    'localtime' NOT in available_timezones()
PYTHONTZPATH="" + no wheel       -> ZoneInfoNotFoundError at first resolution (fails loud)
default TZPATH                   -> system tree searched FIRST; this host: 498 zones,
                                    US/Eastern and Europe/Kiev absent
ZoneInfo('localtime')            -> accepted, tzname CEST, present in available_timezones()
```

```
%Z: 'IST' Kolkata | 'MSK' Moscow | '-03' Sao_Paulo | '+1030' Lord_Howe | '+0545' Kathmandu
%A/%B follow LC_TIME (container LANG=C.UTF-8 today, so English); %Z does not
fromtimestamp(t, zone) sets fold correctly, so the two folds render as CEST and CET
```

The abbreviation genuinely discriminates the two occurrences of a repeated reading — the design
property holds, and it means the ambiguity disclosure is belt-and-braces rather than the only
signal.

## Settled, no action

Unix time has no leap seconds, and SQLite `REAL` round-trips epoch floats exactly
(`1792888200.123456` → identical), so `due_at` needs no special handling. Monotonic-vs-wall-clock
is `reminder-delivery`'s problem. `ZoneInfo` rejects absolute paths and `..` traversal.
