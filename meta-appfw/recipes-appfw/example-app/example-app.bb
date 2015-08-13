DESCRIPTION = "Example Node application"
HOMEPAGE = "http://example.com"
LICENSE = "MIT"

LIC_FILES_CHKSUM = "file://COPYING.MIT;md5=838c366f69b72c5df05c96dff79b35f2"

DEPENDS = "nodejs-native"
RDEPENDS_${PN} = "nodejs"

SRC_URI = "file://example.js \
           file://package.json \
           file://COPYING.MIT"

IOTAPP_PROVIDER = "example-corp"

# use iot-app support class
inherit iotapp

S = "${WORKDIR}"

# we don't care about debug for the few binary node modules
INHIBIT_PACKAGE_DEBUG_SPLIT = "1"
INSANE_SKIP_${PN} = "staticdev"

# manual install before npm-install.bbclass lands

do_install () {
    mkdir -p ${D}${IOTAPP_INSTALLATION_PATH}/lib/node_modules/${PN}
    cp example.js package.json COPYING.MIT ${D}${IOTAPP_INSTALLATION_PATH}/lib/node_modules/${PN}/
}

FILES_${PN} = "${IOTAPP_INSTALLATION_PATH}/lib/node_modules/example-app"

PACKAGES = "${PN}"

