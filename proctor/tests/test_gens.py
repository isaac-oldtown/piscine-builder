from __future__ import annotations

import struct

import pytest
from proctor.compat import lines
from proctor.errors import Error
from proctor.gens import GenChoice, GenConst, Lib, Rng
from proctor.value import Value, ValueFloat, ValueInt, ValueList, ValueStr

from proctor import gens, yamlio


def g(s: str) -> gens.Gen:
    return gens.parse(s, {})


def test_dsl_parses_and_respects_bounds() -> None:
    rng = Rng.new(7)
    s = g('string "ab," 2..5')
    for _ in range(200):
        v = s.generate(rng)
        assert isinstance(v, ValueStr)
        assert 2 <= len(v.s) <= 5, v.s
        assert all(c in "ab," for c in v.s)
    lst = g("list(int 0..9) 0..3")
    for _ in range(200):
        lv = lst.generate(rng)
        assert isinstance(lv, ValueList)
        assert len(lv.items) <= 3
        assert all(isinstance(x, ValueInt) and 0 <= x.i <= 9 for x in lv.items)
    c = g('choice ["INFO", "WARN"]')
    assert isinstance(c, GenChoice) and len(c.values) == 2
    k = g("lit 42")
    assert isinstance(k, GenConst) and isinstance(k.value, ValueInt) and k.value.i == 42
    with pytest.raises(Error):
        gens.parse("nope", {})


def test_templates_and_lib_refs() -> None:
    lib: Lib = yamlio.load('hhmm: { fmt: "{h}:{m}", vars: { h: int 0..23 w2, m: int 0..59 w2 } }\n')
    gl = gens.parse("lines(hhmm) 3..3", lib)
    v = gl.generate(Rng.new(1))
    assert isinstance(v, ValueStr)
    assert len(lines(v.s)) == 3
    assert all(len(line) == 5 and line[2:3] == ":" for line in lines(v.s)), v.s


def test_shrinking_moves_towards_small_values() -> None:
    s = g("string 0..10")
    c = s.shrink(ValueStr("hello"))
    assert c[0] == ValueStr("")
    i = g("int 5..100")
    assert i.shrink(ValueInt(50))[0] == ValueInt(5)


# ---------------------------------------------------------------------------
# Sequences pinned from the Rust tools (students replay failures with
# `proctor check --seed <hex>`, so the same seed must give the same inputs).


def show(v: Value) -> str:
    if isinstance(v, ValueFloat):
        return "f" + format(struct.unpack("<Q", struct.pack("<d", v.f))[0], "x")
    if isinstance(v, ValueList):
        return "[" + ", ".join(show(x) for x in v.items) + "]"
    return str(v)


def bits(x: float) -> str:
    return format(struct.unpack("<Q", struct.pack("<d", x))[0], "x")


NEXT = {
    0: [
        0xE220A8397B1DCDAF,
        0x6E789E6AA1B965F4,
        0x06C45D188009454F,
        0xF88BB8A8724C81EC,
        0x1B39896A51A8749B,
    ],
    1: [
        0x910A2DEC89025CC1,
        0xBEEB8DA1658EEC67,
        0xF893A2EEFB32555E,
        0x71C18690EE42C90B,
        0x71BB54D8D101B5B9,
    ],
    42: [
        0xBDD732262FEB6E95,
        0x28EFE333B266F103,
        0x47526757130F9F52,
        0x581CE1FF0E4AE394,
        0x09BC585A244823F2,
    ],
    0xDEADBEEF: [
        0x4ADFB90F68C9EB9B,
        0xDE586A3141A10922,
        0x021FBC2F8E1CFC1D,
        0x7466CE737BE16790,
        0x3BFA8764F685BD1C,
    ],
    (1 << 64) - 1: [
        0xE4D971771B652C20,
        0xE99FF867DBF682C9,
        0x382FF84CB27281E9,
        0x6D1DB36CCBA982D2,
        0xB4A0472E578069AE,
    ],
}


def test_rng_matches_rust() -> None:
    for seed, want in NEXT.items():
        r = Rng.new(seed)
        assert [r.next_u64() for _ in range(5)] == want, seed
    r = Rng.new(7)
    assert [r.below(10) for _ in range(8)] == [7, 4, 6, 3, 4, 5, 8, 2]
    assert [r.range(-5, 5) for _ in range(8)] == [2, 4, -3, 4, -3, 2, 4, 1]
    assert [bits(r.unit()) for _ in range(4)] == [
        "3fece19adbeba939",
        "3feebb03e66743f7",
        "3fb3a16201310d98",
        "3fda0d026c7cbed4",
    ]
    lo, hi = -(1 << 127) // 2, ((1 << 127) - 1) // 2
    assert [r.range(lo, hi) for _ in range(3)] == [
        51670229469222892273278308533004411933,
        75248607538250640756304887530606164140,
        45319986637659948262776002121769604306,
    ]
    assert [r.chance(0.3) for _ in range(8)] == [
        False,
        False,
        True,
        False,
        True,
        True,
        False,
        False,
    ]


def test_sub_seed_matches_rust() -> None:
    assert gens.sub_seed(42, "test") == 3800222841740446480
    assert gens.sub_seed(0, "") == 14087677454934409008
    assert gens.sub_seed(1, "fuzz top") == 17914632681150605071
    assert gens.sub_seed(0xFFFF, "é") == 17714032602146368257


HHMM_LIB = 'hhmm: { fmt: "{h}:{m}", vars: { h: int 0..23 w2, m: int 0..59 w2 } }\n'

# (spec, describe, first 12 values with `Rng::new(sub_seed(0x1234, spec))`)
SEQUENCES = [
    (
        'string "ab" 0..10',
        'string 0..10 over "ab"',
        [
            '"a"',
            '""',
            '""',
            '"bba"',
            '"a"',
            '"ab"',
            '""',
            '"aaa"',
            '"bba"',
            '"aababaabaa"',
            '"b"',
            '""',
        ],
    ),
    (
        "int -5..5",
        "int -5..5",
        ["0", "-4", "5", "4", "-1", "-4", "-4", "3", "1", "-4", "2", "5"],
    ),
    (
        "list(int 0..9) 0..5",
        "list 0..5 of (int 0..9)",
        ["[]", "[4]", "[0]", "[]", "[0]", "[9]", "[]", "[0]", "[9]", "[1, 5, 6]", "[]", "[0, 8]"],
    ),
    (
        "float -1..1",
        "float -1..1",
        [
            "f3fc4c5e35973d600",
            "f3fce54f1de1fb250",
            "fbfefc0236ecfe4aa",
            "fbfdb533df09f50f4",
            "fbfee5ab13c8c7bf2",
            "fbfbdf0a26b2fa5b0",
            "fbfefc0d7199daf0e",
            "fbfd75f83001c1590",
            "f3fdb65571e8f9aa0",
            "f3ff0000000000000",
            "f3fee05a6072a96d8",
            "fbfc4acd7b93b4bb8",
        ],
    ),
    (
        "word",
        'string 1..8 over "abcdefghijklmnopqrstuvwxyz"',
        [
            '"b"',
            '"sccra"',
            '"oqoalh"',
            '"zqzz"',
            '"koy"',
            '"f"',
            '"y"',
            '"rp"',
            '"dvdddddv"',
            '"y"',
            '"p"',
            '"vko"',
        ],
    ),
    (
        "lines(hhmm) 0..4",
        '0..4 lines of (text "{int 0..23 (width 2)}:{int 0..59 (width 2)}")',
        [
            '"05:17\\n"',
            '"22:09\\n22:16\\n"',
            '"22:17\\n"',
            '""',
            '"01:04\\n"',
            '"00:39\\n"',
            '"19:46\\n"',
            '""',
            '"04:09\\n06:48\\n05:06\\n18:16\\n"',
            '""',
            '""',
            '"08:49\\n"',
        ],
    ),
    (
        "opt(int 0..3)",
        "(int 0..3) or null",
        ["1", "2", "3", "null", "2", "1", "null", "1", "3", "0", "2", "1"],
    ),
    (
        "char",
        'char from " !\\"#$%&\'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\\\]^_`abcdefghijklmnopqrstuvwxyz{|}~"',
        ["'1'", "'%'", "'w'", "'n'", "'U'", "'.'", "'4'", "'i'", "'y'", "'c'", "'z'", "'N'"],
    ),
    (
        "bool",
        "bool",
        [
            "false",
            "false",
            "false",
            "false",
            "true",
            "true",
            "false",
            "false",
            "true",
            "true",
            "false",
            "true",
        ],
    ),
    (
        'choice ["a", "b", 3]',
        'one of ["a", "b", 3]',
        ['"b"', '"b"', '"b"', '"a"', '"a"', "3", '"a"', '"b"', "3", '"b"', '"a"', '"b"'],
    ),
    (
        "ascii 0..6",
        'string 0..6 over " !\\"#$%&\'()*+,-./0123456789:;<=>?@ABCDEFG..."',
        [
            '"."',
            '"kWYDN?"',
            '"U!"',
            '"y"',
            '""',
            '"X"',
            '"PV"',
            '"n9"',
            '"HX"',
            '"X<"',
            '"n>"',
            '"?w"',
        ],
    ),
    (
        "uint",
        "int 0..1000",
        ["285", "492", "548", "1", "255", "0", "1", "620", "495", "999", "271", "393"],
    ),
    (
        "int -50..99 w3",
        "int -50..99 (width 3)",
        ["18", "-50", "19", "78", "99", "99", "-39", "90", "90", "61", "10", "61"],
    ),
    (
        "string",
        'string 0..20 over "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN..."',
        [
            '"LZ Q"',
            '""',
            '"ZRhd!FHbf6HT"',
            '""',
            '"lZVpwP"',
            '"Hf3"',
            '"Bw"',
            '"O-U:z"',
            '""',
            '"?dzwHZ"',
            '"P,krI"',
            '""',
        ],
    ),
]


@pytest.mark.parametrize(("spec", "description", "values"), SEQUENCES)
def test_generated_sequences_match_rust(spec: str, description: str, values: list[str]) -> None:
    lib: Lib = yamlio.load(HHMM_LIB)
    gen = gens.parse(spec, lib)
    assert gens.describe(gen) == description
    rng = Rng.new(gens.sub_seed(0x1234, spec))
    assert [show(gen.generate(rng)) for _ in range(12)] == values


def test_lib_template_describe() -> None:
    lib: Lib = yamlio.load(HHMM_LIB)
    assert (
        gens.describe(gens.parse("hhmm", lib))
        == 'text "{int 0..23 (width 2)}:{int 0..59 (width 2)}"'
    )


SHRINKS: list[tuple[str, Value, list[str]]] = [
    (
        "string 0..10",
        ValueStr("hello"),
        [
            '""',
            '"he"',
            '"lo"',
            '"ello"',
            '"hllo"',
            '"helo"',
            '"helo"',
            '"hell"',
            '"aello"',
            '"hallo"',
            '"healo"',
            '"helao"',
            '"hella"',
        ],
    ),
    ("int 5..100", ValueInt(50), ["5", "27", "49"]),
    ("int -20..20", ValueInt(-13), ["0", "-6", "-12"]),
    (
        "list(int 0..9) 1..5",
        ValueList([ValueInt(3), ValueInt(1), ValueInt(4)]),
        [
            "[3]",
            "[4]",
            "[1, 4]",
            "[3, 4]",
            "[3, 1]",
            "[0, 1, 4]",
            "[1, 1, 4]",
            "[2, 1, 4]",
            "[3, 0, 4]",
            "[3, 1, 0]",
            "[3, 1, 2]",
            "[3, 1, 3]",
        ],
    ),
    (
        "lines(word) 0..5",
        ValueStr("ab\ncd\nef\n"),
        ['""', '"ab\\n"', '"ef\\n"', '"cd\\nef\\n"', '"ab\\nef\\n"', '"ab\\ncd\\n"'],
    ),
    ("opt(int 0..9)", ValueInt(7), ["null", "0", "3", "6"]),
]


@pytest.mark.parametrize(("spec", "value", "want"), SHRINKS)
def test_shrinks_match_rust(spec: str, value: Value, want: list[str]) -> None:
    assert [show(x) for x in g(spec).shrink(value)] == want


ERRORS = [
    ("nope", "unknown generator `nope`"),
    ("list", "`list` needs an inner generator: list(...)"),
    ("choice", 'choice needs a non-empty list: choice ["a", "b"]'),
    ("int 1..x", "bad range `1..x`"),
    ("int zz", 'unexpected `zz` (expected "alphabet", lo..hi, wN or [list])'),
    ("(int)", "generator `(int)` must start with a name"),
    ("list(int 0..3", "unbalanced parentheses in `list(int 0..3`"),
    ("lines(nope)", "unknown generator `nope`"),
    ("w12 0..3", "unknown generator `w12`"),
]


@pytest.mark.parametrize(("spec", "msg"), ERRORS)
def test_errors_match_rust(spec: str, msg: str) -> None:
    lib: Lib = yamlio.load(HHMM_LIB)
    with pytest.raises(Error) as e:
        gens.parse(spec, lib)
    assert e.value.msg == msg


MAPS = (
    'a: { fmt: "{x}-{{y}}" }\n'
    "b: { vars: {} }\n"
    'c: { fmt: "[{x}] {y}!", vars: { x: int 1..2, y: [1, 2.5, true, null, "s"] } }\n'
    "d: 3\n"
    "e: 2.5\n"
    "f: true\n"
    "g: ~\n"
)

MAP_RESULTS: dict[str, str | tuple[str, list[str]]] = {
    "a": "fmt uses `{x}` but `vars` does not define it",
    "b": 'a generator map needs `fmt: "...{var}..."` and `vars:`',
    "c": (
        'text "[{int 1..2}] {one of [1, 2.5, true, null, "s"]}!"',
        ['"[1] s!"', '"[1] 1!"', '"[1] true!"', '"[2] true!"'],
    ),
    "d": ("always 3", ["3", "3", "3", "3"]),
    "e": ("always 2.5", ["f4004000000000000"] * 4),
    "f": ("always true", ["true"] * 4),
    "g": "unsupported generator value",
}


def test_yaml_values_and_templates_match_rust() -> None:
    maps: Lib = yamlio.load(MAPS)
    for k, v in sorted(maps.items()):
        want = MAP_RESULTS[k]
        if isinstance(want, str):
            with pytest.raises(Error) as e:
                gens.parse(v, maps)
            assert e.value.msg == want, k
            continue
        gen = gens.parse(v, maps)
        rng = Rng.new(9)
        assert (gens.describe(gen), [show(gen.generate(rng)) for _ in range(4)]) == want, k
