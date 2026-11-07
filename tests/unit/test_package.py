import glyphrift


def test_version_is_a_nonempty_string() -> None:
    assert isinstance(glyphrift.__version__, str)
    assert glyphrift.__version__
