"""M1.1: account limits, validation, interactive prompt, saving to config.yaml."""
import pytest
import yaml

from nsebt.__main__ import main
from nsebt.backtest import account as A
from nsebt.backtest.account import Account, AccountError


def test_defaults():
    a = Account()
    assert (a.capital, a.max_positions, a.max_position_pct, a.max_participation_pct) == (36000, 3, 33, 5)


@pytest.mark.parametrize("v", [0, -1, -36000, 1_000_000_001, 5e9, "abc", float("nan")])
def test_capital_rejected(v):
    with pytest.raises(AccountError):
        Account(capital=v)


@pytest.mark.parametrize("v,want", [(1_000_000_000, 1e9), ("36,000", 36000), ("₹ 50,000", 50000), ("1e5", 1e5), (0.5, 0.5)])
def test_capital_accepted(v, want):
    assert Account(capital=v).capital == want


@pytest.mark.parametrize("field,v", [("max_positions", 0), ("max_positions", 101), ("max_positions", 2.5),
                                     ("max_position_pct", 0.5), ("max_position_pct", 101),
                                     ("max_participation_pct", 0), ("max_participation_pct", 150)])
def test_out_of_range_rejected(field, v):
    with pytest.raises(AccountError):
        Account(**{field: v})


def test_percent_sign_and_int_parse():
    a = Account(max_positions="5", max_position_pct="20%")
    assert a.max_positions == 5 and isinstance(a.max_positions, int) and a.max_position_pct == 20


def test_prompt_enter_keeps_defaults():
    asked = []
    a = A.prompt(Account(capital=50000), ask=lambda q: asked.append(q) or "", say=lambda m: None)
    assert a == Account(capital=50000) and len(asked) == 4
    assert "[50,000]" in asked[0]


def test_prompt_reasks_until_valid():
    answers = iter(["2000000000", "-5", "75,000", "", "40", ""])
    said = []
    a = A.prompt(None, ask=lambda q: next(answers), say=said.append)
    assert a.capital == 75000 and a.max_positions == 3 and a.max_position_pct == 40
    assert len(said) == 2 and "at most 1,000,000,000" in said[0]


def test_override_and_config_roundtrip(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("# top comment\npaths:\n  store: data/store   # keep me\n\nbackup:\n  destinations: []\n")
    assert A.from_config(yaml.safe_load(cfg.read_text())) is None
    A.save(cfg, Account(capital=100000, max_positions=5))
    raw = yaml.safe_load(cfg.read_text())
    assert A.from_config(raw) == Account(capital=100000, max_positions=5)
    A.save(cfg, Account(capital=200000))                      # replaces, does not duplicate
    text = cfg.read_text()
    assert text.count("account:") == 1 and "# keep me" in text and "# top comment" in text
    assert A.from_config(yaml.safe_load(text)).capital == 200000
    assert A.from_config(yaml.safe_load(text)).with_overrides(capital=None, max_positions=7).max_positions == 7


def test_account_block_in_middle_is_replaced(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("paths:\n  store: s\n\naccount:\n  capital: 1000\n  max_positions: 2\n\nbackup:\n  destinations: []\n")
    A.save(cfg, Account(capital=5000))
    raw = yaml.safe_load(cfg.read_text())
    assert raw["account"]["capital"] == 5000 and raw["backup"] == {"destinations": []} and raw["paths"] == {"store": "s"}


def test_unknown_key_in_config_rejected():
    with pytest.raises(AccountError):
        A.from_config({"account": {"capital": 1000, "max_postions": 3}})


def test_cli_flags_validate_and_save(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("paths:\n  store: s\n")
    main(["--config", str(cfg), "account", "--capital", "250000", "--max-positions", "4"])
    assert A.from_config(yaml.safe_load(cfg.read_text())) == Account(capital=250000, max_positions=4)
    with pytest.raises(SystemExit) as e:
        main(["--config", str(cfg), "account", "--capital", "2000000000"])
    assert "at most" in str(e.value.code)
    assert A.from_config(yaml.safe_load(cfg.read_text())).capital == 250000     # unchanged


def test_cli_prompt(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("paths:\n  store: s\n")
    answers = iter(["40000", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda q: next(answers))
    main(["--config", str(cfg), "account"])
    assert A.from_config(yaml.safe_load(cfg.read_text())) == Account(capital=40000)
