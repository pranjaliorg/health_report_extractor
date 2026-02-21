from db import engine, Base
import models

Base.metadata.create_all(engine)

print("Table created")