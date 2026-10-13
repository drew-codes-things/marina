const fs = require('fs');
const path = require('path');

exports.default = async function addX11Launcher(context) {
  if (context.electronPlatformName !== 'linux') return;
  const name = context.packager.executableName || 'marina';
  const launcher = path.join(context.appOutDir, name);
  const real = `${launcher}.bin`;
  fs.renameSync(launcher, real);
  fs.writeFileSync(launcher, `#!/bin/sh
here=$(dirname "$(readlink -f "$0")")
if [ -n "$MARINA_WAYLAND" ]; then exec "$here/${name}.bin" "$@"; fi
exec "$here/${name}.bin" --ozone-platform=x11 "$@"
`, { mode: 0o755 });
};
