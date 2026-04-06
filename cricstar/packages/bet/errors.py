class BetError(Exception):
    @property
    def error_message(self) -> str:
        return "An unknown bet error occurred."


class LockedError(BetError):
    @property
    def error_message(self) -> str:
        return "Your proposal is locked and cannot be edited."


class AlreadyLockedError(BetError):
    @property
    def error_message(self) -> str:
        return "That cricketer is already locked in another trade or bet."


class NotProposedError(BetError):
    @property
    def error_message(self) -> str:
        return "That cricketer is not in your proposal."


class OwnershipError(BetError):
    @property
    def error_message(self) -> str:
        return "That cricketer does not belong to you."


class NotTradeableError(BetError):
    @property
    def error_message(self) -> str:
        return "That cricketer cannot be traded or bet."


class CancelledError(BetError):
    @property
    def error_message(self) -> str:
        return "The bet has been cancelled."


class IntegrityError(BetError):
    @property
    def error_message(self) -> str:
        return "Card ownership changed during the bet — it has been cancelled for safety."
