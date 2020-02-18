import numpy as np
import cv2
from scipy import interpolate
from numba import jit


class EngineLineCropper(object):

    def __init__(self, correct_slant=False, line_height=32, poly=0, scale=1, blend_border=5):
        self.correct_slant = correct_slant
        self.line_height = line_height
        self.poly = poly
        self.scale = scale
        self.blend_border = blend_border

    @jit
    def reverse_value_mapping(self, forward_mapping, sample_positions, sampled_values):
        backward_mapping = np.zeros_like(sample_positions)
        forward_position = 0
        for i in range(sample_positions.shape[0]):
            while forward_mapping[forward_position] > sample_positions[i]:
                forward_position += 1
            d = forward_mapping[forward_position] - forward_mapping[forward_position-1]
            da = (sample_positions[i] - forward_mapping[forward_position-1]) / d
            backward_mapping[i] = (1 - da) * sampled_values[forward_position - 1] + da * sampled_values[forward_position]
        return backward_mapping

    def get_crop_inputs(self, img, baseline, line_heights, target_height):
        line_heights = [line_heights[0], line_heights[1]]

        coords = np.asarray(baseline).copy().astype(int)
        if self.poly:
            if coords.shape[0] > 2:
                line_interpf = np.poly1d(np.polyfit(coords[:,1], coords[:,0], self.poly))
            else:
                line_interpf = interpolate.interp1d(coords[:,1], coords[:,0], kind='linear',)
        else:
            try:
                line_interpf = interpolate.interp1d(coords[:,1], coords[:,0], kind='cubic',)
            except: # fall back to linear interpolation in case y_values fails (usually with very short baselines)
                line_interpf = interpolate.interp1d(coords[:,1], coords[:,0], kind='linear',)

        left = coords[:, 1].min()
        right = coords[:, 1].max()
        line_x_values = np.arange(left, right)
        line_y_values = line_interpf(line_x_values) # positions in source
        line_length = ((line_x_values[:-1] - line_x_values[1:])**2 + (line_y_values[:-1] - line_y_values[1:])**2) ** 0.5
        mapping_x_to_line_pos = np.concatenate([np.zeros(1), np.cumsum(line_length)]) # mapping of source to t

        scale = target_height / (line_heights[0] + line_heights[1])

        horizontal_sample_count = int(mapping_x_to_line_pos[-1] * scale) # number of target samples

        tmp = np.linspace(0, mapping_x_to_line_pos[-1], horizontal_sample_count)
        output_x_positions = self.reverse_value_mapping( # get source x baseline positions in target pixels
            mapping_x_to_line_pos, tmp, line_x_values)

        output_y_positions = line_interpf(output_x_positions) # get source baseline y positions in target pixels

        d_x = np.full_like(output_x_positions, 0.1)
        d_y = output_y_positions - line_interpf(output_x_positions + 0.1)
        norm_scales = (d_x**2 + d_y**2) ** 0.5 # get normals

        norm_x = -d_y / norm_scales
        norm_y = d_x / norm_scales

        vertical_map = np.linspace(-line_heights[0], line_heights[1], target_height).reshape(-1, 1)
        vertical_map_x = norm_x.reshape(1, -1) * vertical_map + output_x_positions.reshape(1, -1) # get the rest of source x positions for target pixels computed from normals
        vertical_map_y = norm_y.reshape(1, -1) * vertical_map + output_y_positions.reshape(1, -1) # get the rest of source y positions for target pixels computed from normals

        coords = np.stack((vertical_map_x, vertical_map_y), axis=2).astype(np.float32)

        return coords

    def crop(self, img, baseline, height, return_mapping=False):
        line_coords = self.get_crop_inputs(img, baseline, height, self.line_height)
        line_crop = cv2.remap(img, line_coords[:, :, 0], line_coords[:, :, 1],
                              interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

        if return_mapping:
            raise Exception('Not implemented')
            line_mapping = self.reverse_mapping(coords, img_crop.shape)
            return line_crop_out, line_mapping*fy, offset
        else:
            return line_crop

    def blend_in(self, img, line_crop, mapping, offset):

        y1, x1 = offset
        mapping = np.nan_to_num(mapping).astype(np.float32)

        line_rewarped = cv2.remap(line_crop, mapping[:, :, 1], mapping[:, :, 0], interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_TRANSPARENT)
        img_crop = img[y1:y1+line_rewarped.shape[0], x1:x1+line_rewarped.shape[1]]

        blend_weights_x = np.zeros((mapping.shape[0], mapping.shape[1]))
        blend_weights_y = np.zeros((mapping.shape[0], mapping.shape[1]))
        mapping = np.round(mapping)
        blend_weights_x[mapping[:, :, 0]>self.blend_border] = self.blend_border
        blend_weights_y[mapping[:, :, 1]>self.blend_border] = self.blend_border
        for i in range(self.blend_border+1):
            blend_weights_x[mapping[:, :, 0]==i] = i
            blend_weights_x[mapping[:, :, 0]==(np.amax(mapping[:, :, 0])-i)] = i
            blend_weights_y[mapping[:, :, 1]==i] = i
            blend_weights_y[mapping[:, :, 1]==(np.amax(mapping[:, :, 1])-i)] = i
        blend_weights = np.minimum(blend_weights_x, blend_weights_y) / self.blend_border
        blend_weights = blend_weights[:, :, np.newaxis]

        img_crop = blend_weights * line_rewarped + (1-blend_weights) * img_crop
        img[y1:y1+line_rewarped.shape[0], x1:x1+line_rewarped.shape[1], :] = img_crop.astype(np.uint8)

        return img

    def rigid_crop(self, img, baseline, height):
        line_height = height[1] + height[0]
        dy = 10
        do = height[0] / 10
        up = height[1] / 10

        baseline = np.asarray(baseline)

        one_line_points = []

        p1 = baseline[0, ::-1]
        p2 = baseline[-1, ::-1]
        dir = (p2 - p1)
        dir = dir / (dir ** 2).sum() ** 0.5
        n = dir[::-1] * dy
        n[0] = -n[0]
        p1 = p1 - dir * dy * 0.5
        p2 = p2 + dir * dy * 0.5

        pts1 = np.asarray([p1 - do * n, p2 - do * n, p2 + up * n, p1 - up * n]).astype(np.float32)
        one_line_points.append(np.copy(pts1))
        pts1 = pts1[:3]

        width = ((p2 - p1) ** 2).sum() ** 0.5 / (up + do) / dy * line_height
        pts2 = np.asarray([(0, 0), (width, 0), (width, line_height)]).astype(np.float32)
        pts1 = np.reshape(pts1, [-1, 1, 2])
        pts2 = np.reshape(pts2, [-1, 1, 2])

        T = cv2.getAffineTransform(pts1, pts2)
        line_crop = cv2.warpAffine(img, T, (int(width + 0.5), int(line_height)))

        return line_crop

    @jit
    def reverse_mapping_fast(self, forward_mapping, shape):
        y_mapping = forward_mapping[:,:,0]
        y_mapping = np.clip(cv2.resize(y_mapping, (0,0), fx=4, fy=4, interpolation=cv2.INTER_LINEAR), 0, shape[0]-1)
        x_mapping = forward_mapping[:,:,1]
        x_mapping = np.clip(cv2.resize(x_mapping, (0,0), fx=4, fy=4, interpolation=cv2.INTER_LINEAR), 0, shape[1]-1)

        y_map = np.tile(np.arange(0, forward_mapping.shape[0]), (forward_mapping.shape[1], 1)).T.astype(np.float32)
        y_map = cv2.resize(y_map, (0,0), fx=4, fy=4, interpolation=cv2.INTER_LINEAR)
        x_map = np.tile(np.arange(0, forward_mapping.shape[1]), (forward_mapping.shape[0], 1)).astype(np.float32)
        x_map = cv2.resize(x_map, (0,0), fx=4, fy=4, interpolation=cv2.INTER_LINEAR)

        reverse_mapping = np.ones((shape[0], shape[1], 2), dtype=np.float32) * -1

        for sx, sy, dx, dy in zip(x_map.flatten(), y_map.flatten(), x_mapping.astype(np.int32).flatten(), y_mapping.astype(np.int32).flatten()):
            reverse_mapping[dy, dx, 0] = sx
            reverse_mapping[dy, dx, 1] = sy

        return reverse_mapping

    @jit
    def reverse_mapping(self, forward_mapping, shape, interpolator_step=8):
        x_map = np.tile(np.arange(0, forward_mapping.shape[0]), (forward_mapping.shape[1], 1)).T.astype(np.float32)
        y_map = np.tile(np.arange(0, forward_mapping.shape[1]), (forward_mapping.shape[0], 1)).astype(np.float32)
        points = list(zip(forward_mapping[:, :, 1].flatten(), forward_mapping[:, :, 0].flatten()))

        values_x = x_map.flatten().tolist()
        values_y = y_map.flatten().tolist()
        reverse_mapper_x = interpolate.LinearNDInterpolator(points[::interpolator_step], values_x[::interpolator_step])
        reverse_mapper_y = interpolate.LinearNDInterpolator(points[::interpolator_step], values_y[::interpolator_step])

        x_map_reverse = np.tile(np.arange(0, shape[0]), (shape[1], 1)).T
        y_map_reverse = np.tile(np.arange(0, shape[1]), (shape[0], 1))

        line_mapping = np.zeros((shape[0], shape[1], 2))
        new_points = list(zip(x_map_reverse.flatten(), y_map_reverse.flatten()))
        line_mapping[x_map_reverse.flatten(), y_map_reverse.flatten(), 0] = reverse_mapper_x(new_points)
        line_mapping[x_map_reverse.flatten(), y_map_reverse.flatten(), 1] = reverse_mapper_y(new_points)

        return line_mapping
