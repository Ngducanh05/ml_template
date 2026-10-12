"""ECR is the authoritative eligible-runtime catalog; no catalog state mirror."""
import json
from .state_v2 import DIGEST, REPOSITORY, runtime_image

REGISTRY = "596432515999"
REPO_NAME = "model-service"


def fingerprint_tag(fingerprint):
    if not isinstance(fingerprint, str) or not DIGEST.fullmatch(fingerprint):
        raise ValueError("Invalid technical fingerprint")
    return "runtime-fp-" + fingerprint[7:]


class Catalog:
    def __init__(self, ecr):
        self.ecr = ecr

    def verify_repository(self):
        repo = self.ecr.describe_repositories(registryId=REGISTRY, repositoryNames=[REPO_NAME])["repositories"]
        if (len(repo) != 1 or repo[0].get("repositoryUri") != REPOSITORY
                or repo[0].get("imageTagMutability") != "IMMUTABLE"
                or repo[0].get("imageTagMutabilityExclusionFilters")):
            raise ValueError("Catalog requires immutable tags without exclusions")

    def lookup(self, fingerprint):
        tag = fingerprint_tag(fingerprint)
        self.verify_repository()
        try:
            result = self.ecr.describe_images(registryId=REGISTRY, repositoryName=REPO_NAME,
                                               imageIds=[{"imageTag": tag}])
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "ImageNotFoundException":
                return None
            raise
        rows = result.get("imageDetails", [])
        if len(rows) != 1 or tag not in rows[0].get("imageTags", []):
            raise ValueError("Malformed ECR catalog response")
        return runtime_image(REPOSITORY + "@" + rows[0].get("imageDigest", ""))

    def publish(self, fingerprint, image):
        """Caller must supply independently verified exact-image eligibility evidence."""
        runtime_image(image)
        existing = self.lookup(fingerprint)
        if existing is not None:
            if existing != image:
                raise ValueError("Catalog integrity violation")
            return existing
        digest = image.split("@", 1)[1]
        response = self.ecr.batch_get_image(registryId=REGISTRY, repositoryName=REPO_NAME,
                                            imageIds=[{"imageDigest": digest}])
        rows = response.get("images", [])
        if response.get("failures") or len(rows) != 1 or rows[0]["imageId"]["imageDigest"] != digest:
            raise ValueError("Source manifest unavailable")
        manifest = rows[0]["imageManifest"]
        json.loads(manifest)
        try:
            self.ecr.put_image(registryId=REGISTRY, repositoryName=REPO_NAME,
                               imageManifest=manifest, imageDigest=digest,
                               imageTag=fingerprint_tag(fingerprint))
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code not in ("ImageAlreadyExistsException", "ImageTagAlreadyExistsException"):
                raise
        if self.lookup(fingerprint) != image:
            raise ValueError("Catalog publication conflict")
        return image
