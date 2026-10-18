LEVELS = ("INFO", "WARN", "ERROR")


def _shaped(text, pattern):
    if len(text) != len(pattern):
        return False
    for c, p in zip(text, pattern):
        if p == "9" and not c.isdigit():
            return False
        if p != "9" and c != p:
            return False
    return True


def parse_line(line):
    parts = line.strip().split(None, 3)
    if len(parts) < 4:
        return None
    date, time, level, msg = parts
    if not _shaped(date, "9999-99-99") or not _shaped(time, "99:99:99"):
        return None
    if level not in LEVELS:
        return None
    return {"time": date + " " + time, "level": level, "msg": msg.strip()}
