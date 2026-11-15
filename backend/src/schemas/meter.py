from pydantic import BaseModel, Field


class MeterIngestInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    source: str = Field(default='manual.json', max_length=200)
    request_token: str | None = Field(default=None, max_length=64)
    readings: list[dict] = Field(min_length=1, max_length=20000)


class MeterMergeInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    source: str = Field(default='meter-published.json', max_length=200)
    dataset_id: int
    include_isolated: bool = Field(default=False)
