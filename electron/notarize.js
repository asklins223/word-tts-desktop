const path = require('path');

exports.default = async function notarizeMacBuild(context) {
    if (context.electronPlatformName !== 'darwin') return;

    const appleId = process.env.APPLE_ID;
    const appleIdPassword = process.env.APPLE_APP_SPECIFIC_PASSWORD;
    const teamId = process.env.APPLE_TEAM_ID;
    if (!appleId || !appleIdPassword || !teamId) {
        console.log('[notarize] 未配置 Apple 公证凭据，跳过公证');
        return;
    }

    const appName = context.packager.appInfo.productFilename;
    const appPath = path.join(context.appOutDir, `${appName}.app`);
    const { notarize } = require('@electron/notarize');
    console.log(`[notarize] 正在公证 ${appPath}`);
    await notarize({
        appPath,
        appleId,
        appleIdPassword,
        teamId,
    });
    console.log('[notarize] Apple 公证完成');
};
