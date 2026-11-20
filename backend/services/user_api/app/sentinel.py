class Unset:
    """Sentinel — отличает 'поле отсутствует в запросе' от 'явный null'."""

    def __bool__(self) -> bool:
        return False


UNSET = Unset()
