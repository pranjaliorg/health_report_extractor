from sqlalchemy import Column, Integer, Text
from db import Base

class Report(Base):
    __tablename__ = "report_table"

    id = Column(Integer, primary_key=True)
    patient_name = Column(Text)
    admitted_date = Column(Text)
    discharged_date = Column(Text)
    discharge_notes = Column(Text)