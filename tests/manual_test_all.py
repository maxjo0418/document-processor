import json
from os import listdir
from pathlib import Path

from document_processor import DocIR, configure_logging, get_logger
from pydantic import BaseModel


configure_logging(level="INFO")
logger = get_logger(__name__)

doc_dir = Path("doc_samples/new_test")
out_dir = Path("results")

files = [doc_dir / file for file in listdir(doc_dir)]


for file_ in files:
    logger.info("Processing %s", file_)
    doc = DocIR.from_file(file_)

    # == Add metadata == #

    class MyMetaData(BaseModel):
        a: int = 1
        b: str = "test"

    metainfo = MyMetaData(a=1)
    
    # ex: add metadata to all runs
    for para in doc.paragraphs:
        for run in para.iter_all_runs():
            run.meta = metainfo

    # ================== #


    with \
        open((out_dir / file_.stem).with_suffix(".json"), "w", encoding="utf-8") as json_f, \
        open((out_dir / file_.stem).with_suffix(".html"), "w", encoding="utf-8") as html_f:
        
        json.dump(doc.model_dump(mode="json"), json_f, indent=4, ensure_ascii=False)
        html_f.write(doc.to_html())

    logger.info("Completed %s", file_)
