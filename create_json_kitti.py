import json, os
### Create json file for all training images to generate instance map for each image

def load_all_image_paths(image_dir):
    #Subdir
    city_dir = os.listdir(image_dir)
    city_dir.sort()
    city_dir = city_dir[:-3]
    image = []
    for i in range(len(city_dir)):
        frame_dir = image_dir + city_dir[i] + "/image_02/data/"
        frame_list = os.listdir(frame_dir)
        frame_list.sort()
        for j in range(len(frame_list)):
            full_image_path = frame_dir + frame_list[j]   
            assert os.path.isfile(full_image_path)    
            image.append(full_image_path)
               
    return image



root = "/disk1/yue/kitti/raw_data_256p/val/"
data = {}
data['images'] = []
cnt = 0
all_images = load_all_image_paths(root)
for i in range(len(all_images)):
    if i >=0 and i <= 100:
        data['images'].append({
        'id': i,
        'height': 375,
        'width': 1242,
        'file_name': all_images[i]
            })
with open('kitti_10_03.json', 'w') as outfile:
    json.dump(data, outfile)
