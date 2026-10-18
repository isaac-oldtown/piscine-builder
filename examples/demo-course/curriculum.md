---
title: PiscineBuilder demo course
level: beginner
hours_per_week: 10
weeks: 1
---

## d01-string-basics: String Basics
goal: Re-implement three small string helpers using only raw memory and pointers.
language: c
concept_difficulty: 3
difficulty: 2
hours: 2.5
done_when: str_len, str_dup and str_split give the right results on empty strings and repeated separators, and return NULL without leaking when an allocation fails
introduces: [splitting text into words, pointer arithmetic, manual allocation]
target: function
scaffold: minimal
allows: [stdlib.h, stddef.h, malloc, free]

Functions `str_len`, `str_dup` and `str_split` in `src/`, declared in `include/str_basics.h`. Words are the maximal runs of characters other than the separator. On allocation failure, free everything already allocated and return NULL: the tests make malloc fail on purpose.

## d02-word-count: Word Count in Bash
goal: Write a Bash script that counts the words it reads on standard input.
language: bash
concept_difficulty: 1
difficulty: 1
hours: 1
done_when: "`wc.sh` prints the number of words of its standard input, or the number of lines with `-l`, and exits with 0"
introduces: [reading standard input, exit codes]
assumes: [splitting text into words]
target: program
scaffold: none
allows: []

No external commands: only Bash built-ins. Creating `wc.sh` is part of the exercise.

## d03-longest-word: Longest Word
goal: Find the longest word of a text and return it as a slice of the input.
language: rust
concept_difficulty: 2
difficulty: 2
hours: 1.5
done_when: longest_word returns the first longest word as a slice of its input, or None for a text without words, and word_count agrees with it
introduces: [optional results, borrowed slices]
assumes: [splitting text into words]
target: function
scaffold: minimal
allows: []

Length counts characters, not bytes. Returning a slice of the input, rather than a new String, is the point of the exercise.

## d04-reverse-lines: Reverse Lines
goal: Print every line of standard input reversed.
language: perl
concept_difficulty: 1
difficulty: 1
hours: 1
stretch: true
done_when: every input line comes out reversed, in order, including a last line without a newline
introduces: [line-oriented filters]
assumes: [reading standard input]
target: program
scaffold: minimal
allows: [strict, warnings]

Optional: a first look at a language declared as data rather than built into the grader.

## p01-logstat: Log Toolkit
goal: Build `logstat`, a command-line tool that reads a log and reports what happened in it.
minimum: "`python -m logstat` reads a log on standard input, counts the entries of each level and reports malformed lines"
language: python
difficulty: 3
assumes: [splitting text into words, optional results, reading standard input, exit codes]

In scope: one log format (`YYYY-MM-DD HH:MM:SS LEVEL message`), the levels INFO, WARN and ERROR, counting per level. Out of scope: time ranges, other formats, files given as arguments.

### p01.1-parsing: Parsing
goal: Turn one raw log line into a structured entry.
concept_difficulty: 2
difficulty: 2
hours: 1.5
done_when: parse_line returns time, level and message for a valid line and None for each kind of malformed line
introduces: [parsing structured lines]
assumes: [splitting text into words, optional results]
target: function
scaffold: minimal
allows: []

`logstat.parse_line(line)` returns a dict with `time`, `level` and `msg`. Malformed: fewer than four fields, a badly shaped date or time, an unknown level. Keep it a pure function: the next stage reuses it.

### p01.2-cli: Command Line
goal: Read a log on standard input and print how many entries each level has.
concept_difficulty: 2
difficulty: 3
hours: 2
done_when: "`python -m logstat` prints `LEVEL count` lines in the order INFO, WARN, ERROR, skips malformed lines, and exits with 1 when it skipped any"
introduces: [command-line programs]
assumes: [parsing structured lines, reading standard input, exit codes]
target: program
scaffold: minimal
allows: [sys]

Build on parse_line from p01.1: its tests run again at this stage. Empty lines are ignored; other malformed lines are counted and reported on stderr as `skipped N malformed line(s)`.
