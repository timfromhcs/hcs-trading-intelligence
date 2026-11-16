# Third-party license inventory

CI collects Python license metadata and generates a CycloneDX inventory from the Python and npm lockfiles. Review the inventory with each release. Bundled dependencies retain their own license terms.

PyInstaller 6.22.3 is a pinned build tool. Its upstream GPL bootloader exception permits distribution of the generated application bundle under the application's license, subject to the bundled dependencies' licenses. The license gate records PyInstaller's installed metadata and recognizes this reviewed version. Review the exception again before upgrading the tool. [PyInstaller license](https://www.pyinstaller.org/en/stable/license.html)
