"""Tests for the coding_repo fixture package."""

from pkg.core import leaf, mid
from pkg.hub import hub_entry, HubService


def test_leaf():
    assert leaf(2) == 4


def test_mid():
    assert mid(2) == 5


def test_hub_entry():
    assert hub_entry(1) == leaf(mid(1))


def test_hub_service():
    assert HubService().run(1) == leaf(mid(1))
