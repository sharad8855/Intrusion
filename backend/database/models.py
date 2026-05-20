"""SQLAlchemy ORM models: Camera, Zone, Alert."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Column, DateTime, Float, ForeignKey, Integer,
                         String)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Camera(Base):
    """
    Camera record — camera-add logic mirrors the cctv-management project.

    `url` is a single smart field. It accepts:
      * a full URL          ->  rtsp://admin:Baap%40123@192.168.2.99:554/stream1
      * a host (+ path)     ->  192.168.2.99/stream1
      * a local webcam idx  ->  0
    The connectable URL is produced by `build_rtsp()`.
    """

    __tablename__ = "cameras"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    url = Column(String(400), nullable=False)  # IP / host[/path] OR full URL
    port = Column(String(10))                  # string, like cctv-management
    username = Column(String(120))
    password = Column(String(400))             # stored encrypted (ENC: prefix)
    latitude = Column(Float)
    longitude = Column(Float)
    status = Column(String(20), default="offline")   # online | offline | error
    enabled = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)

    zones = relationship("Zone", back_populates="camera",
                          cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="camera",
                          cascade="all, delete-orphan")

    def build_rtsp(self) -> str | None:
        """
        Decrypt the stored password and build a connectable RTSP URL.

        Same flow as cctv-management: decrypt -> `build_rtsp_url()`.
        """
        from backend.core.security import build_rtsp_url, decrypt_value

        plain_pwd = decrypt_value(self.password) if self.password else self.password
        return build_rtsp_url(self.url, self.port, self.username, plain_pwd)

    def as_dict(self) -> dict:
        # NOTE: password is never exposed by the API.
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "port": self.port,
            "username": self.username,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "status": self.status,
            "enabled": bool(self.enabled),
        }


class Zone(Base):
    __tablename__ = "zones"

    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"), nullable=False)
    zone_type = Column(String(20), nullable=False)        # line | circle | polygon
    name = Column(String(120), default="zone")
    coordinates = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    camera = relationship("Camera", back_populates="zones")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "zone_type": self.zone_type,
            "name": self.name,
            "coordinates": self.coordinates,
        }


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"), nullable=False)
    zone_id = Column(Integer)
    alert_type = Column(String(20))            # line | circle | polygon
    label = Column(String(40))                 # detected class
    track_id = Column(Integer)
    image_path = Column(String(400))
    notified = Column(Integer, default=0)      # WhatsApp sent?
    timestamp = Column(DateTime, default=datetime.utcnow)

    camera = relationship("Camera", back_populates="alerts")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "zone_id": self.zone_id,
            "alert_type": self.alert_type,
            "label": self.label,
            "track_id": self.track_id,
            "image_path": self.image_path,
            "notified": bool(self.notified),
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }
