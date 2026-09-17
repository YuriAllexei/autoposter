car_photos/ holds ONE sub-folder per car. Folder names must sort in the same
order the cars appear in post.txt — use numeric prefixes:

  data/car_photos/
  ├── 01_tacoma_2021/
  │   ├── 1.jpg
  │   ├── 2.jpg
  │   └── ...
  ├── 02_corolla_2019/
  └── ...

Rules the poster enforces:
- cars are iterated in sorted folder-name order; photos inside each car folder
  in sorted file-name order (jpg/jpeg/png/webp per AP_PHOTO_EXTENSIONS).
- total photos uploaded = sum over cars, capped by AP_MAX_PHOTOS_PER_POST.
- only image files; hidden files and .gitkeep are ignored.
