from pkg.clean import clean_token
from pkg.tokens import normalize_tokens

def test_ascii():
    assert clean_token("  hi  ") == "hi"

def test_nbsp():
    assert clean_token("\u00a0hi\u00a0") == "hi"

def test_em_space():
    assert clean_token("\u2003x\u2003") == "x"

def test_list():
    assert normalize_tokens([" a ", "\u00a0b\u00a0", " "]) == ["a", "b"]
