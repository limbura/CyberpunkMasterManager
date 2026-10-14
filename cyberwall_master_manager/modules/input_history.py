"""История одного поля; состояние просмотра живёт только до Enter."""

class HistoryCursor:
    def __init__(self, entries):
        self.entries = list(entries)
        self.index = len(self.entries)
        self.draft = ""

    def move(self, direction, current):
        if not self.entries:
            return current
        if self.index == len(self.entries):
            self.draft = current
        self.index = max(0, min(len(self.entries), self.index + direction))
        return self.draft if self.index == len(self.entries) else self.entries[self.index]
