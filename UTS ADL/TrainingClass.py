import tensorflow
import h5py
import numpy as np
import os
import sys
import signal
import shutil
import importlib.util
import time
import cv2

from keras.preprocessing.image import ImageDataGenerator, array_to_img, img_to_array, load_img
from keras.models import Model, Sequential, load_model
from keras.layers import * 
from keras import backend as K
from keras import losses
from keras.optimizers import *
from keras.callbacks import ModelCheckpoint, LearningRateScheduler, CSVLogger, TensorBoard, EarlyStopping
from keras.metrics import categorical_accuracy
from utils import shuffle_together_simple, random_crop
from random import randint
import imgaug as ia
from tensorflow.keras.utils import to_categorical
from tensorflow.keras.optimizers import Adam, RMSprop
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from imgaug import augmenters as iaa
from imgaug import parameters as iap

def fancy_loss(y_true,y_pred):
    "This function has been written in tensorflow, needs some little changes to work with keras"    
    y_pred = tf.reshape(y_pred,[-1,y_pred.shape[-1]])
    y_true = tf.argmax(y_true, axis=-1)
    y_true = tf.reshape(y_true,[-1])
    return lovasz_softmax_flat(y_pred, y_true)



class TrainingClass:
    
    def __init__(self, name, model_path, data_path, save_folder, no_epochs, kernel_size, batch_size, filters, lrate = 1e-4, reg = 0.0001,  loss = 'categorical_crossentropy', duplicate = True ):
        self.name = name
        self.model_path = model_path
        self.data_path = data_path
        self.save_folder = save_folder
        self.kernel_size = kernel_size
        self.batch_size = batch_size
        self.filters = filters
        self.lrate = lrate
        self.reg = reg
        self.no_epochs = no_epochs
        if(loss == "fancy"):
            from fancyloss import lovasz_softmax_flat
            self.loss = fancy_loss
        elif(loss == "jaccard"):
            from jaccard_loss import jaccard_distance
            self.loss = jaccard_distance
        else:
            self.loss = loss
        self.load_data()
        #if(duplicate == True):
        #    self.train, self.train_label, self.train_bodypart = self.duplicate()
        #    print("Finished duplicating images, the final size of your training set is %d images."%self.train.shape[0])
        self.write_metadata()
        self.compile()
        
    def load_data(self):
      def center_crop(img, dim):
        """Returns center cropped image

        Args:
        img: image to be center cropped
        dim: dimensions (width, height) to be cropped from center
        """
        width, height = img.shape[1], img.shape[0]
        #process crop width and height for max available dimension
        crop_height = dim[0] if dim[0]<img.shape[1] else img.shape[1]
        crop_width = dim[1] if dim[1]<img.shape[0] else img.shape[0] 

        mid_x, mid_y = int(width/2), int(height/2)
        cw2, ch2 = int(crop_width/2), int(crop_height/2) 
        crop_img = img[mid_y-ch2:mid_y+ch2, mid_x-cw2:mid_x+cw2]
        return crop_img
        
      label_training =[]
      data_training=[]
      data_dir = "/content/ADL/UTS ADL/data_training"
      label_dir = "/content/ADL/UTS ADL/label"
      data_list = sorted(os.listdir(data_dir)[:45])
      label_list = sorted(os.listdir(label_dir)[:45])

      for img_name in data_list:
        label = cv2.imread(label_dir+"/"+img_name[:-4]+"_label.png")
        data = cv2.imread(data_dir+"/"+img_name)
        width, height, channel = data.shape
        label = center_crop(label, (width,height))
        label = cv2.resize(label,(200,200))
        data = cv2.resize(data,(200,200))
        data = cv2.cvtColor(data, cv2.COLOR_BGR2GRAY)
        label_training.append(label)
        data_training.append(data)
        
      data_training = np.asarray(data_training)
      label_training = np.asarray(label_training)
      n, h, w = data_training.shape
      data_training = data_training.reshape(n, h, w, 1)
      label_training = label_training.reshape(n, h, w, 3)

      self.train = data_training[:35]
      self.no_images, self.height, self.width, self.channels= self.train.shape
      self.train_label = label_training[:35]
      #self.train_bodypart = hf['train_bodypart'][:]
      self.no_images, _, _, self.no_classes = self.train_label.shape
      self.val = data_training[35:40]
      self.val_label = label_training[35:40]
      self.val_label = self.val_label.reshape((-1,self.height*self.width,self.no_classes))
      print("Data loaded succesfully.")

        
    def write_metadata(self):
        "Writes metadata to a txt file, with all the training information"
        metafile_path = self.save_folder + "/metadata.txt"

        if (os.path.isfile(metafile_path)):
            confirm_metada = input("Warning metadata file exists, continue? (y/n) ")
            if(confirm_metada == "y"):
                shutil.rmtree(metafile_path)
            else:
                sys.exit()
                
        metadata = open(metafile_path, "w")
        metadata.write("name: %s \n"%self.name)
        metadata.write("Data: %s \n"%self.data_path)
        metadata.write("kernel_size: %d \n" %self.kernel_size)
        metadata.write("batch_size:%d \n" %self.batch_size)
        metadata.write("filters %s \n" %(self.filters,))
        metadata.write("lrate: %f \n" %self.lrate)
        metadata.write("reg: %f \n" %self.reg)
        metadata.write("Loss function: %s \n" %self.loss)
        metadata.write("no_epochs: %d \n" %self.no_epochs)
        metadata.close()
                

        
    def generator(self):
        "This generator is used to feed the data to the training algorithm. Given a batch size, randomly divides the training data into batches. This function allows training even when all the data cannot be loaded into RAM memory."
        
        while True:
            indices = np.asarray(range(0, self.no_images))
            np.random.shuffle(indices)
            for idx in range(0, len(indices), self.batch_size):
                batch_indices = indices[idx:idx+self.batch_size]
                batch_indices.sort()
                batch_indices = batch_indices.tolist()
                by = self.train_label[batch_indices]
                by = by.reshape(-1, self.width*self.height, self.no_classes)
                bx = self.train[batch_indices]
                
                yield(bx,by)
   
    def augmentator(self, index):
        " This function defines the trainsformations to apply on the images, and if required on the labels"

        translate_max = 0.01
        rotate_max = 15
        shear_max = 2

        affine_trasform = iaa.Affine( translate_percent={"x": (-translate_max, translate_max),
                                                         "y": (-translate_max, translate_max)}, # translate by +-
                                      rotate=(-rotate_max, rotate_max), # rotate by -rotate_max to +rotate_max degrees
                                      shear=(-shear_max, shear_max), # shear by -shear_max to +shear_max degrees
                                      order=[1], # use nearest neighbour or bilinear interpolation (fast)
                                      cval=125, # if mode is constant, use a cval between 0 and 255
                                      mode="reflect",
                                      #mode = "",
                                      name="Affine",
                                     )


        spatial_aug = iaa.Sequential([iaa.Fliplr(0.5), iaa.Flipud(0.5), affine_trasform])

        other_aug = iaa.SomeOf((1, None),
                [
                    iaa.OneOf([
                        iaa.GaussianBlur((0, 0.4)), # blur images with a sigma between 0 and 1.0
                        iaa.ElasticTransformation(alpha=(0.5, 1.5), sigma=0.25), # very few

                    ]),

                ])

        '''
        affine_trasform = iaa.Affine( translate_percent={"x": (-translate_max, translate_max),
                                                         "y": (-translate_max, translate_max)}, # translate by +-
                                      rotate=(-rotate_max, rotate_max), # rotate by -rotate_max to +rotate_max degrees
                                      shear=(-shear_max, shear_max), # shear by -shear_max to +shear_max degrees
                                      order=[1], # use nearest neighbour or bilinear interpolation (fast)
                                      cval=125, # if mode is constant, use a cval between 0 and 255
                                      mode="reflect",
                                      name="Affine",
                                     )


        spatial_aug = iaa.Sequential([iaa.Fliplr(0.5), iaa.Flipud(0.5), affine_trasform])

        sometimes = lambda aug: iaa.Sometimes(0.5, aug)

        other_aug = iaa.SomeOf((1, None),
                [
                    iaa.OneOf([
                        iaa.GaussianBlur((0, 0.4)), # blur images with a sigma between 0 and 1.0
                    ]),

                ])

        elastic_aug = iaa.SomeOf((1, None),
                [
                    iaa.OneOf([
                        sometimes(iaa.ElasticTransformation(alpha=(50, 60), sigma=16)), # move pixels locally around (with random strengths)
                    ]),

                ])

        
        # Defines augmentations to perform on the images and their labels
        augmentators = [spatial_aug,other_aug, elastic_aug]
        spatial_det = augmentators[0].to_deterministic()
        # to deterministic is needed to apply exactly the same spatial transformation to the data and the labels
        other_aug = augmentators[1]
        # When only adding noise there's no need to perform the transformation on the label
        elastic_det = augmentators[2].to_deterministic()
    
        image_aug = spatial_det.augment_image(self.train[index])
        label_aug = spatial_det.augment_image(255*self.train_label[index])

        image_aug = elastic_det.augment_image(image_aug)
        label_aug = elastic_det.augment_image(label_aug)
 
        img_crop, label_crop = random_crop(image_aug,label_aug,0.,0.4)
        image_aug = other_aug.augment_image(img_crop )    

        label_aug = label_crop
        
          
        label_aug = to_categorical(np.argmax(label_aug,axis=-1), num_classes = 3) # only needed if performing elastic transformations
        # Otherwise careful, returns [255,0,0] not [1,0,0] !
        '''
        augmentator = [spatial_aug,other_aug]
        spatial_det = augmentator[0].to_deterministic() 
        other_det = augmentator[1]

        image_aug = spatial_det.augment_image(self.train[index])
        label_aug = spatial_det.augment_image(self.train_label[index])
        img_crop, label_crop = random_crop(image_aug,label_aug,0.1,0.4)
        image_aug = other_det.augment_image(img_crop )               
        label_aug = to_categorical(np.argmax(label_crop,axis=-1), num_classes = self.no_classes)
        return image_aug, label_aug

    def generator_with_augmentations(self):
        "This generator is used to feed the data to the training algorithm. Given a batch size, randomly divides the training data into batches and augment each image once randomly. "
        batch_images = np.zeros((self.batch_size, self.width, self.height, 1))
        batch_labels = np.zeros((self.batch_size, self.width*self.height, self.no_classes))	# X and Y coordinates
        while True:
            indices = np.asarray(range(0, self.no_images))
            np.random.shuffle(indices)
            for idx in range(0, len(indices), self.batch_size):
                batch_indices = indices[idx:idx+self.batch_size]
                batch_indices.sort()
                batch_indices = batch_indices.tolist()
                for i, idx2 in enumerate(batch_indices):
                    augmented_image, augmented_label = self.augmentator(idx)
                    augmented_label = augmented_label.reshape(self.width*self.height, self.no_classes)
                    batch_images[i] = augmented_image
                    batch_labels[i] = augmented_label

                yield (batch_images,batch_labels)

    def compile(self):
        spec = importlib.util.spec_from_file_location("module.name", self.model_path)
        print(self.model_path)
        self.model_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.model_module)
        #self.model = self.model_module.model(l2_lambda = self.reg, input_shape = (self.height, self.width, self.channels), classes = self.no_classes, kernel_size = self.kernel_size, filter_depth = self.filters)
        self.model = self.model_module.model(input_shape = (self.height, self.width, self.channels), classes = self.no_classes, kernel_size = self.kernel_size, filter_depth = self.filters)
        self.model.compile(optimizer = RMSprop(lr = self.lrate, decay = 1e-6), loss = self.loss, metrics = ['accuracy'])
        #self.model.compile(optimizer = Adam(lr = self.lrate), loss = self.loss, metrics = ['accuracy'])
        #self.model.compile(optimizer = SGD(lr = self.lrate, momentum = 0.9, nesterov = True), loss = self.loss, metrics = ['accuracy'])
        print("Model loaded and compiled succesfully.")
        
    def fit(self):
        csv_logger = CSVLogger(self.save_folder + "/" + self.name + ".csv")
        #save_path = self.name + "_{epoch:03d}.h5"
        save_path = self.name + ".h5"
        save_path = self.save_folder + "/" + save_path
        earlystop = EarlyStopping(monitor="val_loss", min_delta = 0, patience = 20, verbose = 1, mode = 'min') 
        checkpoint = ModelCheckpoint(save_path, monitor = "val_loss", verbose = 1, save_best_only = True, save_weights_only = False, mode = "auto", period = 1)
        #tb = TensorBoard(log_dir = os.path.join(self.save_folder,'tboard'), batch_size = 1, write_graph = True, write_images = False)

        self.model.fit_generator(self.generator_with_augmentations(), steps_per_epoch = self.no_images // self.batch_size, epochs = self.no_epochs, callbacks = [csv_logger, checkpoint, earlystop], validation_data = (self.val, self.val_label))
        #self.model.fit_generator(self.generator(), steps_per_epoch = self.no_images // self.batch_size, epochs = self.no_epochs, callbacks = [csv_logger, checkpoint, earlystop], validation_data = (self.val, self.val_label))
    
        
        
