from sqlalchemy import String, JSON, Double, Integer, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class MeterBatch(Base):
    """一次原始电表记录接入批次：draft 可复核，published 才参与归因，重发后旧批次 superseded。"""
    __tablename__ = 'meter_batches'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    source: Mapped[str] = mapped_column(String(200), default='manual.json')
    status: Mapped[str] = mapped_column(String(20), default='draft')
    meters: Mapped[list] = mapped_column(JSON, default=list)
    lines: Mapped[list] = mapped_column(JSON, default=list)
    affected_days: Mapped[list] = mapped_column(JSON, default=list)
    preview: Mapped[dict] = mapped_column(JSON, default=dict)
    checksum: Mapped[str] = mapped_column(String(64))
    request_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    published_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class MeterReading(Base):
    """带电表、时刻、读数的原始记录，只追加不修改；重新发布以新批次重建。"""
    __tablename__ = 'meter_readings'
    __table_args__ = (UniqueConstraint('batch_id', 'meter_id', 'read_time', name='uq_reading_batch_meter_time'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('meter_batches.id', ondelete='CASCADE'))
    meter_id: Mapped[str] = mapped_column(String(80))
    line: Mapped[str] = mapped_column(String(80))
    read_time: Mapped[str] = mapped_column(String(40))
    reading: Mapped[float] = mapped_column(Double)
    event: Mapped[str | None] = mapped_column(String(20), nullable=True)
    handoff: Mapped[float | None] = mapped_column(Double, nullable=True)


class MeterState(Base):
    """每块电表的当前发布代次与读数边界；发布时锁定该行串行化并发批次。

    lock_version 提供乐观并发：行锁缺失的引擎（如 SQLite）上，
    并发发布只有一个条件更新能成功，另一个回滚，保证每电表仅一个 published 批次。
    """
    __tablename__ = 'meter_states'
    meter_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    line: Mapped[str] = mapped_column(String(80))
    generation: Mapped[int] = mapped_column(Integer, default=1)
    lock_version: Mapped[int] = mapped_column(Integer, default=0)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey('meter_batches.id'), nullable=True)
    first_time: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_time: Mapped[str | None] = mapped_column(String(40), nullable=True)
    first_reading: Mapped[float | None] = mapped_column(Double, nullable=True)
    last_reading: Mapped[float | None] = mapped_column(Double, nullable=True)


class MeterSegment(Base):
    """相邻读数区间：complete 可出日耗，gap 跨日缺测不均摊，isolated 回零/换表缺交接隔离。"""
    __tablename__ = 'meter_segments'
    __table_args__ = (UniqueConstraint('meter_id', 'generation', 'start_time', name='uq_segment_meter_gen_start'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('meter_batches.id', ondelete='CASCADE'))
    meter_id: Mapped[str] = mapped_column(String(80))
    line: Mapped[str] = mapped_column(String(80))
    generation: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[str] = mapped_column(String(40))
    end_time: Mapped[str] = mapped_column(String(40))
    start_reading: Mapped[float] = mapped_column(Double)
    end_reading: Mapped[float] = mapped_column(Double)
    delta: Mapped[float] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(200), default='')
    start_reading_id: Mapped[int | None] = mapped_column(ForeignKey('meter_readings.id'), nullable=True)
    end_reading_id: Mapped[int | None] = mapped_column(ForeignKey('meter_readings.id'), nullable=True)


class MeterDaily(Base):
    """已发布日耗：每表每日仅一行，日耗等于两个有效读数之差，读数依据可追查。"""
    __tablename__ = 'meter_daily'
    __table_args__ = (UniqueConstraint('meter_id', 'day', name='uq_daily_meter_day'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('meter_batches.id', ondelete='CASCADE'))
    meter_id: Mapped[str] = mapped_column(String(80))
    line: Mapped[str] = mapped_column(String(80))
    day: Mapped[str] = mapped_column(String(10))
    kwh: Mapped[float] = mapped_column(Double)
    generation: Mapped[int] = mapped_column(Integer)
    start_reading_id: Mapped[int] = mapped_column(ForeignKey('meter_readings.id'))
    end_reading_id: Mapped[int] = mapped_column(ForeignKey('meter_readings.id'))
    start_time: Mapped[str] = mapped_column(String(40))
    end_time: Mapped[str] = mapped_column(String(40))
    start_reading: Mapped[float] = mapped_column(Double)
    end_reading: Mapped[float] = mapped_column(Double)
    created_at: Mapped[float] = mapped_column(Double)
