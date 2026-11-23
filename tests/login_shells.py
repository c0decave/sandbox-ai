"""How a login shell that is not POSIX reads the one command line ssh hands it (`$SHELL -c LINE`), for the hosts tests
when fish or csh is not installed: words(SHELL, LINE) -> the argv of the one simple command LINE is, or ValueError
where that shell would refuse LINE or read more than one simple command in it (what the model leaves out: variables,
pipes, lists, globs, redirections - none of them is in a line that is no more than words).

fish 3 (doc_src/language.rst, "Quotes"): inside single quotes only \\' and \\\\ are escapes (a backslash before any
other character stays); inside double quotes \\", \\\\, \\$ and a backslash-newline are, and $ expands; outside
quotes a backslash escapes the next character. csh and tcsh: inside single quotes nothing is an escape, and a line
break there is an error ("Unmatched '."); outside quotes a backslash escapes the next character.
Standard library only."""

SPECIAL = set(";|&<>()$*?[]{}~#`\n")


def words(shell, line):
    if shell not in ("fish", "csh"):
        raise ValueError(f"no model of {shell}")
    found, word, in_word, index = [], [], False, 0
    while index < len(line):
        c = line[index]
        if c in " \t":
            if in_word:
                found.append("".join(word))
                word, in_word = [], False
            index += 1
            continue
        in_word = True
        if c == "'":
            index += 1
            while True:
                if index >= len(line):
                    raise ValueError(f"{shell}: unterminated quote")
                c = line[index]
                if c == "'":
                    index += 1
                    break
                if shell == "csh" and c == "\n":
                    raise ValueError("csh: Unmatched '.")
                if shell == "fish" and c == "\\" and index + 1 < len(line) and line[index + 1] in "'\\":
                    word.append(line[index + 1])
                    index += 2
                    continue
                word.append(c)
                index += 1
        elif c == '"':
            index += 1
            while True:
                if index >= len(line):
                    raise ValueError(f"{shell}: unterminated quote")
                c = line[index]
                if c == '"':
                    index += 1
                    break
                if c == "$":
                    raise ValueError(f"{shell}: a variable (not modelled)")
                if shell == "csh" and c == "\n":
                    raise ValueError('csh: Unmatched ".')
                if c == "\\" and shell == "fish" and index + 1 < len(line) and line[index + 1] in '"\\$\n':
                    if line[index + 1] != "\n":
                        word.append(line[index + 1])
                    index += 2
                    continue
                word.append(c)
                index += 1
        elif c == "\\":
            if index + 1 >= len(line):
                raise ValueError(f"{shell}: a backslash at the end")
            word.append(line[index + 1])
            index += 2
        elif c in SPECIAL:
            raise ValueError(f"{shell}: {c!r} outside quotes: not one simple command")
        else:
            word.append(c)
            index += 1
    if in_word:
        found.append("".join(word))
    return found
