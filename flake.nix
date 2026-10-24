{
  description = "rekordbox-mcp fork with standalone Nix packaging";
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/02c087f3fe673c9e317f206a627cb6b910336dbe";
    
  };
  outputs = { self, nixpkgs, ... }:
    let
      systems = [ "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      packageFor = system:
        let pkgs = import nixpkgs { inherit system; };
        in pkgs.callPackage ./nix/package.nix {
          pyrekordbox = pkgs.python3Packages.callPackage ./nix/pyrekordbox {};
        };
    in {
      packages = forAllSystems (system:
        let package = packageFor system;
        in { default = package; rekordbox-mcp = package; });
      checks = forAllSystems (system: { rekordbox-mcp = packageFor system; });
    };
}
