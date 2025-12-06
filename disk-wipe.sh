###“BMH-style” wipe in a single loop, which clears both the beginning and the end of each disk, just like automatedCleaningMode=metadata would:


for disk in $(lsblk -ndo NAME,TYPE,RO | awk '$2=="disk" && $3==0 {print "/dev/"$1}'); do
  echo "Wiping $disk ..."
  # remove filesystem signatures
  wipefs -a $disk || true
  # remove GPT/MBR partition tables
  sgdisk --zap-all $disk || true
  # zero out first 1GB
  dd if=/dev/zero of=$disk bs=10M count=100 oflag=direct || true
  # zero out last 1MB
  size=$(blockdev --getsz $disk)
  dd if=/dev/zero of=$disk bs=512 seek=$((size - 2048)) count=2048 oflag=direct || true
done
