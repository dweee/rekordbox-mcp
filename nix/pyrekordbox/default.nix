{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  setuptools,
  setuptools-scm,
  bidict,
  construct,
  numpy,
  packaging,
  psutil,
  sqlalchemy,
  sqlcipher3-wheels,
  python-dateutil,
}:
buildPythonPackage rec {
  pname = "pyrekordbox";
  version = "0.4.4";
  format = "pyproject";

  src = fetchFromGitHub {
    owner = "dylanljones";
    repo = "pyrekordbox";
    rev = "v${version}";
    # To get the correct hash, you can run:
    # nix-prefetch-url --unpack https://github.com/dylanljones/pyrekordbox/archive/refs/tags/v0.4.4.tar.gz
    hash = "sha256-eOr48xAmW73HL/CQu4a10IEZ6okpixGWYWJdqzwxKPQ=";
  };

  patches = [
    ./pyproject-updates.patch
  ];

  # Since fetchFromGitHub does not include the .git directory,
  # setuptools-scm needs the version to be explicitly provided.
  SETUPTOOLS_SCM_PRETEND_VERSION = version;

  nativeBuildInputs = [
    setuptools
    setuptools-scm
  ];

  propagatedBuildInputs = [
    bidict
    construct
    numpy
    packaging
    psutil
    sqlalchemy
    sqlcipher3-wheels
    python-dateutil
  ];

  # Tests are disabled by default as they require specific database state and test data
  doCheck = false;

  meta = with lib; {
    description = "Unofficial Python package for interacting with the library of Pioneer's Rekordbox DJ software";
    homepage = "https://github.com/dylanljones/pyrekordbox";
    license = licenses.mit;
    maintainers = []; # Add maintainers if desired
  };
}
