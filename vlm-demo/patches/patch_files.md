# Patch Files

## Purpose

Contains commands to be run for creating patch files for differences in a file with git apply.

## Prerequisites

- Must know prior the target differences in a file.

> **Acknowledgement:** Commands below were written by Generative AI.

## Code Sequence

### 1. Change to repository root directory and create a temp folder

```bash
cd path/to/repo_root_directory

mkdir -p tmp/
```

### 2. Copy unmodified file into the tmp/ folder

On all three terminals:

```bash
cp path/to/unmodified_file tmp/unmodified_file
```
Be sure not to include ```/``` before tmp!

### 3. Make changes to unmodified file in its original directory

Manually make the desired changes to the previously unmodified file. Make the changes to the file that is in its original directory.

### 4. Create the patch file by applying git diff to the modified file

Apply ```git diff``` to the now modified version of the file

```bash
git diff -- path/to/modified_file \
  > path/to/patch_file.patch
```

### 5. Revert changes of the modified file in its original directory

Verify that the patch is in the correct directory and that it documents the correct file and the correct changes to the file. Then, revert the modified file back to its original version in order to test the patch.

```bash
git checkout -- path/to/modified_file
```

### 6. Verify the target file is back to its original version and delete temp folder

Check that the target file is back to its previous unmodified version. Then, delete the temp folder.

```bash
rm -rf /tmp
```
