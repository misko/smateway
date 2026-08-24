import pytest

from smateway.cli import _board_root


def test_board_id_cannot_escape_state_root() -> None:
    with pytest.raises(ValueError, match="board ID"):
        _board_root("../../tmp")


def test_board_id_accepts_recorded_uid() -> None:
    path = _board_root("stm32c011-4c0055000950313950363920")

    assert path.name == "stm32c011-4c0055000950313950363920"
