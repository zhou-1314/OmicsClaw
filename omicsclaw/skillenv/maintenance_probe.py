"""Selected Python process: inspect offline wheel dependencies or verify imports."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import sys
from pathlib import Path


def inspect(request):
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
    from pip._vendor.packaging.version import Version

    distributions = {}
    for distribution in importlib.metadata.distributions():
        declared = distribution.metadata.get("Name")
        if not declared:
            continue
        name = canonicalize_name(declared)
        if name not in distributions:
            distributions[name] = {
                "version": distribution.version,
                "requires": distribution.requires or [],
            }
        if len(distributions) > 2048:
            raise ValueError("inventory_limit")
    environment = {**default_environment(), "extra": ""}

    def errors(inventory):
        problems = []
        for name, package in sorted(inventory.items()):
            for spelling in package["requires"]:
                requirement = Requirement(spelling)
                if requirement.marker and not requirement.marker.evaluate(environment):
                    continue
                dependency = canonicalize_name(requirement.name)
                actual = inventory.get(dependency)
                if actual is None or not requirement.specifier.contains(
                    actual["version"], prereleases=True
                ):
                    problems.append(
                        [
                            name,
                            dependency,
                            str(requirement.specifier),
                            actual["version"] if actual else None,
                        ]
                    )
                if len(problems) > 128:
                    raise ValueError("dependency_limit")
        return problems

    baseline = errors(distributions)
    planned = dict(distributions)
    tags = set(sys_tags())
    packages = []
    for item in request["packages"]:
        name, version, _, wheel_tags = parse_wheel_filename(item["filename"])
        if (
            name != canonicalize_name(item["name"])
            or Version(item["version"]) != version
            or not tags.intersection(wheel_tags)
        ):
            raise ValueError("wheel_incompatible")
        if item["requiresPython"] and not SpecifierSet(item["requiresPython"]).contains(
            environment["python_full_version"]
        ):
            raise ValueError("python_incompatible")
        if any(Requirement(value).url for value in item["requires"]):
            raise ValueError("url_dependency_unsupported")
        packages.append(
            {
                "name": item["name"],
                "beforeVersion": distributions.get(name, {}).get("version"),
                "afterVersion": item["version"],
            }
        )
        planned[name] = {"version": item["version"], "requires": item["requires"]}
    new_errors = [item for item in errors(planned) if item not in baseline]
    return {
        "prefix": sys.prefix,
        "executable": sys.executable,
        "installerVersion": importlib.metadata.version("pip"),
        "packages": packages,
        "baselineDependencyErrors": baseline,
        "dependencyErrors": new_errors,
    }


def verify(request):
    checked = []
    prefix = Path(request["prefix"]).resolve(strict=True)
    for item in request["checks"]:
        distribution = importlib.metadata.distribution(item["package"])
        module = importlib.import_module(item["module"])
        location = getattr(module, "__file__", None)
        if (
            distribution.version != item["version"]
            or not location
            or not Path(location).resolve(strict=True).is_relative_to(prefix)
        ):
            raise ValueError("package_verification_failed")
        checked.append(
            {
                "package": item["package"],
                "version": distribution.version,
                "modulePath": str(Path(location).resolve()),
            }
        )
    inspection = inspect({"packages": []})
    new_errors = [
        item
        for item in inspection["baselineDependencyErrors"]
        if item not in request["baselineDependencyErrors"]
    ]
    if new_errors:
        raise ValueError("dependency_verification_failed")
    return {"prefix": sys.prefix, "checks": checked, "dependencyErrors": []}


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    result = inspect(request) if request["action"] == "inspect" else verify(request)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
