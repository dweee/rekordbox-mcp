{
  lib,
  python3,
  pyrekordbox,
  ffmpeg,
  chromaprint,
}:
python3.pkgs.buildPythonApplication {
  pname = "rekordbox-mcp";
  version = "1.0.0-flakey";
  pyproject = true;
  src = lib.cleanSource ../.;
  build-system = [ python3.pkgs.hatchling ];
  dependencies = with python3.pkgs; [
    fastmcp
    pyrekordbox
    pydantic
    loguru
    mutagen
  ];
  nativeCheckInputs =
    (with python3.pkgs; [
      pytestCheckHook
      pytest-asyncio
    ])
    ++ [
      ffmpeg
      chromaprint
    ];
  pythonImportsCheck = [
    "rekordbox_mcp.server"
    "rekordbox_mcp.relink"
  ];
  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    (lib.makeBinPath [
      ffmpeg
      chromaprint
    ])
  ];
  meta = {
    description = "Rekordbox MCP with reviewed track relinking and audio inspection";
    homepage = "https://github.com/davehenke/rekordbox-mcp";
    license = lib.licenses.mit;
    mainProgram = "rekordbox-mcp";
    platforms = lib.platforms.darwin;
  };
}
