from pathlib import Path
import pandas as pd

DATA = Path(__file__).parent
open_items = pd.read_csv(DATA / "open_line_items_synthetic.csv", dtype=str)
materials = pd.read_csv(DATA / "material_data_synthetic.csv", dtype=str)
wht_map = pd.read_csv(DATA / "wht_mapping_synthetic.csv", dtype=str)

joined = open_items.merge(materials, on="MaterialData Key", how="left", suffixes=("_line", "_po"))
print("Open-item rows:", len(open_items))
print("Material rows:", len(materials))
print("Joined rows:", len(joined))
print(joined.head(5).to_string())
