"""OpenLigaDB models (https://api.openligadb.de, ODbL): Bundesliga schedule and table."""

from pydantic import BaseModel, ConfigDict, Field

from app.kickbase.models import UtcDatetime


class OldbModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)


class OldbTeam(OldbModel):
    team_id: int = Field(alias="teamId")
    name: str = Field(alias="teamName")
    short_name: str = Field(default="", alias="shortName")


class OldbGroup(OldbModel):
    matchday: int = Field(alias="groupOrderID")
    name: str = Field(default="", alias="groupName")


class OldbMatch(OldbModel):
    id: int = Field(alias="matchID")
    kickoff: UtcDatetime = Field(alias="matchDateTimeUTC")
    group: OldbGroup
    team1: OldbTeam
    team2: OldbTeam
    finished: bool = Field(default=False, alias="matchIsFinished")

    @property
    def matchday(self) -> int:
        return self.group.matchday


class OldbTableRow(OldbModel):
    team_id: int = Field(alias="teamInfoId")
    name: str = Field(alias="teamName")
    short_name: str = Field(default="", alias="shortName")
    points: int
    matches: int
    won: int = 0
    draw: int = 0
    lost: int = 0
    goal_difference: int = Field(default=0, alias="goalDiff")

    @property
    def points_per_game(self) -> float | None:
        return self.points / self.matches if self.matches else None
