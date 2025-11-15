# db/enums.py
import enum

class UserRole(enum.StrEnum):
    ADMIN = "admin"
    CONTESTANT = "contestant"
    UNREGISTERED = "unregistered"

class ContestantRole(enum.StrEnum):
    CAPTAIN = "captain"
    MEMBER = "member"

class SubmissionStatus(enum.StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"

class PageType(enum.StrEnum):
    ABOUT = "about"
    RULES = "rules"
    CUSTOM = "custom"
    INSTRUCTION = "instruction"

class SortDirection(enum.StrEnum):
    ASC = "asc"
    DESC = "desc"

class UiMode(enum.StrEnum):
    HOME = "home"
    TEAM = "team"
    SUBMISSION = "submission"
    SUBMIT = "submit"
    NOTIFICATION = "notification"
    PAGES = "pages"
    COMPETITION = "competition"
    NEW_COMPETITION = "new_competition"
    EDIT_COMPETITION = "edit_competition"
    USER = "user"
    NEW_USER = "new_user"
    EDIT_USER = "edit_user"
    TRACK = "track"
    CHANGE_LANGUAGE = "change_language"
    NEW_TEAM = "new_team"
    EDIT_TEAM = "edit_team"

