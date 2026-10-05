# Kebbi Education Backend v3 - Robot Images + Dynamic Robots

## New
- Each educational robot can have one product image.
- Dashboard: upload/change/delete image inside each robot card.
- Dashboard: `+ Add Robot` creates additional educational robot records without code changes.
- New robot images are stored on the persistent disk under `/var/data/robot_media`.
- Robot endpoint: `GET /api/robot/robot-image?name=...` protected by `X-Robot-Key`.
- Images are served from `/robot-media/<filename>`.
- Schema migrates v2 -> v3 without resetting your current educational-robot edits.

## Deployment
Keep Render persistent disk mounted on `/var/data`.
No new environment variables are required.
Upload the backend folder contents to the repository root as before.

## Dashboard flow
1. Open `/dashboard`.
2. Go to Educational Robots.
3. Open a robot card.
4. Choose an image and press Upload/Change Image.
5. For a new robot press `+ Add Robot`, fill the fields, save, then upload its image.
