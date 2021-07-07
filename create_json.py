import json, os
### Create json file for all training images to generate instance map for each image

def load_all_image_paths(image_dir):
    #Subdir
    city_dir = os.listdir(image_dir)
    city_dir.sort()
    image = []
    for i in range(len(city_dir)):
        frame_dir = image_dir + city_dir[i]
        frame_list = os.listdir(frame_dir)
        frame_list.sort()
        for j in range(len(frame_list)):
            full_image_path = frame_dir + "/" + frame_list[j]   
            assert os.path.isfile(full_image_path)    
            image.append(full_image_path)
               
    return image

data = {}
data['images'] = []
root = "/disk2/yue/cvpr2020_videoprediction/FutureVideoSynthesis/example/cityscapes_example/imgs/val/"
all_images = load_all_image_paths(root)
for i in range(len(all_images)):
    data['images'].append({
    'id': i,
    'height': 1024,
    'width': 2048,
'file_name': all_images[i]
	})

with open('example.json', 'w') as outfile:
    json.dump(data, outfile)
