import os
from datetime import datetime
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urlparse

import requests
from bioimageio.spec import (
    InvalidDescr,
    ResourceDescr,
    ValidationContext,
    load_description,
)
from bioimageio.spec.common import HttpUrl, RelativeFilePath
from bioimageio.spec.utils import download
from dotenv import load_dotenv
from loguru import logger
from ruyaml import YAML
from typing_extensions import Literal

from .generate_collection_json import get_all_versions
from .remote_resource import PublishedVersion
from .s3_client import Client
from .s3_structure.versions import PublishedVersionInfo, Versions

_ = load_dotenv()
yaml = YAML(typ="safe")

ZenodoHost = Literal["https://sandbox.zenodo.org", "https://zenodo.org"]


def backup(client: Client, destination: ZenodoHost):
    """backup all published resources to their own record"""
    if "sandbox" not in destination:
        raise NotImplementedError("impl not production ready")

    all_versions = get_all_versions(client)
    for rid, versions in all_versions.items():
        for p_nr in sorted(versions.published):
            v_info = versions.published[p_nr]
            if v_info.doi is not None:
                continue

            backup_published_version(
                PublishedVersion(client, rid, p_nr),
                v_info,
                destination,
                versions.concept_doi,
            )

        break


def backup_published_version(
    v: PublishedVersion,
    v_info: PublishedVersionInfo,
    destination: ZenodoHost,
    concept_doi: Optional[str],
):
    with ValidationContext(perform_io_checks=False) as val_ctxt:
        rdf = load_description(v.rdf_url)
        rdf_file_name = val_ctxt.file_name

    if isinstance(rdf, InvalidDescr):
        raise Exception(
            "Failed to load RDF from S3:\n" + rdf.validation_summary.format()
        )

    assert rdf_file_name is not None

    if rdf.id is None:
        raise ValueError("Missing bioimage.io `id`")

    if rdf.license is None:
        raise ValueError("Missing license")

    # client = v.client
    headers = {"Content-Type": "application/json"}
    params = {"access_token": os.environ["ZENODO_API_ACCESS_TOKEN"]}

    # List the files at the model URL
    file_urls = v.get_file_urls()
    logger.info("Using file URLs:\n{}", "\n".join((str(obj) for obj in file_urls)))

    if concept_doi is None:
        # get concept_doi again; we may have just overriden it
        concept_doi = v.get_concept_doi()

    if concept_doi is None:
        # Create empty deposition
        r = requests.post(
            f"{destination}/api/deposit/depositions",
            params=params,
            json={},
            headers=headers,
        )
    else:
        concept_id = concept_doi.split("/zenodo.")[1]
        # create a new deposition version with different deposition_id from the existing deposition
        r = requests.post(
            destination
            + "/api/deposit/depositions/"
            + concept_id
            + "/actions/newversion",
            params=params,
        )

    r.raise_for_status()
    deposition_info = r.json()

    bucket_url = deposition_info["links"]["bucket"]

    # # use the new version's deposit link
    # newversion_draft_url = deposition_info["links"]['latest_draft']
    # assert isinstance(newversion_draft_url, str)
    # # Extract nes deposition_id from url
    # deposition_id = newversion_draft_url.split('/')[-1]

    # PUT files to the deposition
    for file_url in file_urls:
        put_file_from_url(file_url, bucket_url, params)

    # Report deposition URL
    deposition_id = deposition_info["id"]
    concept_id = deposition_info["conceptrecid"]
    doi = deposition_info["metadata"]["prereserve_doi"]["doi"]
    concept_doi = doi.replace(deposition_id, concept_id)

    # base_url = f"{destination}/record/{concept_id}/files/"

    metadata = rdf_to_metadata(
        rdf,
        rdf_file_name=rdf_file_name,
        publication_date=v.get_versions().published[v.number].timestamp,
    )

    r = requests.put(
        f"{destination}/api/deposit/depositions/%s" % concept_id,
        params=params,
        json={"metadata": metadata},
        headers=headers,
    )
    r.raise_for_status()
    logger.error("Would be publishing now...(but leaving as draft)")
    # return

    r = requests.post(
        f"{destination}/api/deposit/depositions/{concept_doi}/actions/publish",
        params=params,
    )
    r.raise_for_status()

    if "sandbox" not in destination:
        v.update_versions(
            Versions(
                concept_doi=concept_doi,
                published={
                    v.number: PublishedVersionInfo(
                        sem_ver=v_info.sem_ver,
                        timestamp=v_info.timestamp,
                        status=v_info.status,
                        doi=doi,
                    )
                },
            )
        )


def put_file_from_url(
    file_url: str, destination_url: str, params: Dict[str, Any]
) -> None:
    """Gets a remote file and pushes it up to a destination"""
    filename = PurePosixPath(urlparse(file_url).path).name
    response = requests.get(file_url)
    file_like = BytesIO(response.content)
    put_file(file_like, f"{destination_url}/{filename}", params)
    # TODO: Can we use stream=True and pass response.raw into requests.put?
    #   response = requests.get(file_url, stream=True)
    #   put_file(response.raw, filename, destination_url, params)


def put_file(file_object: BytesIO, url: str, params: Dict[str, Any]):
    r = requests.put(
        url,
        data=file_object,
        params=params,
    )
    r.raise_for_status()


def rdf_authors_to_metadata_creators(rdf: ResourceDescr):
    creators: List[Dict[str, str]] = []
    for author in rdf.authors:
        creator = {
            "name": str(author.name),
            "affiliation": (
                "" if author.affiliation is None else str(author.affiliation)
            ),
        }
        if author.orcid:
            creator["orcid"] = str(author.orcid)

        creators.append(creator)
    return creators


def rdf_to_metadata(
    rdf: ResourceDescr,
    *,
    additional_note: str = "\n(Uploaded via https://bioimage.io)",
    publication_date: datetime,
    rdf_file_name: str,
) -> Dict[str, Any]:

    creators = rdf_authors_to_metadata_creators(rdf)
    docstring_html = ""
    if rdf.documentation is not None:
        docstring = download(rdf.documentation)
        docstring_html = f"<p>{docstring}</p>"

    description = f"""<a href="https://bioimage.io/#/?id={rdf.id}"><span class="label label-success">View on bioimage.io</span></a><br><p>{docstring_html}</p>"""
    keywords = ["bioimage.io", "bioimage.io:" + rdf.type]
    related_identifiers = generate_related_identifiers_from_rdf(rdf, rdf_file_name)
    return {
        "title": rdf.name,
        "description": description,
        "access_right": "open",
        "license": rdf.license,
        "upload_type": "other",
        "creators": creators,
        "publication_date": publication_date.date().isoformat(),
        "keywords": keywords + rdf.tags,
        "notes": rdf.description + additional_note,
        "related_identifiers": related_identifiers,
        "communities": [],
    }


def generate_related_identifiers_from_rdf(rdf: ResourceDescr, rdf_file_name: str):
    related_identifiers: List[Dict[str, str]] = []
    covers = []
    for cover in rdf.covers:
        if isinstance(cover, RelativeFilePath):
            cover = cover.absolute

        assert isinstance(cover, HttpUrl)
        covers.append(str(cover))

        related_identifiers.append(
            {
                "relation": "hasPart",  # is part of this upload
                "identifier": cover,
                "resource_type": "image-figure",
                "scheme": "url",
            }
        )

    for link in rdf.links:
        related_identifiers.append(
            {
                "identifier": f"https://bioimage.io/#/r/{quote_plus(link)}",
                "relation": "references",  # // is referenced by this upload
                "resource_type": "other",
                "scheme": "url",
            }
        )

    related_identifiers.append(
        {
            "identifier": rdf_file_name,
            "relation": "isCompiledBy",  # // compiled/created this upload
            "resource_type": "other",
            "scheme": "url",
        }
    )

    if rdf.documentation is not None:
        related_identifiers.append(
            {
                "identifier": (
                    str(rdf.documentation.absolute)
                    if isinstance(rdf.documentation, RelativeFilePath)
                    else str(rdf.documentation)
                ),
                "relation": "isDocumentedBy",  # is referenced by this upload
                "resource_type": "publication-technicalnote",
                "scheme": "url",
            }
        )
    return related_identifiers
