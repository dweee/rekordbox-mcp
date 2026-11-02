{
  description = "rekordbox-mcp fork with standalone Nix packaging";
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/1c89083e451e891152365b799595cf7790a43325";
    
  };
  outputs = { self, nixpkgs, ... }:
    let
      systems = [ "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      packageFor = system:
        let
          pkgs = import nixpkgs { inherit system; };
          # FastMCP 3 still uses the SDK 1 API; nixpkgs retains it as mcp_1.
          python = pkgs.python3.override {
            packageOverrides = _final: prev: { mcp = prev.mcp_1 or prev.mcp; };
          };
        in pkgs.callPackage ./nix/package.nix {
          python3 = python;
          pyrekordbox = python.pkgs.callPackage ./nix/pyrekordbox {};
        };
    in {
      packages = forAllSystems (system:
        let package = packageFor system;
        in { default = package; rekordbox-mcp = package; });
      checks = forAllSystems (system: { rekordbox-mcp = packageFor system; });
    };
}
